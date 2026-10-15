"""Durable decision holds, independent of execution phases and document drafts."""
from __future__ import annotations

import difflib
import json
import sqlite3
from typing import Mapping
from urllib.parse import quote

from loop_manager_internal.catalog import execplan_drafts


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS execplan_drafts (
            repo TEXT NOT NULL, plan_id TEXT NOT NULL,
            pending INTEGER NOT NULL, evidence TEXT NOT NULL,
            PRIMARY KEY (repo, plan_id)
        )
    """)


def pending_ids(connection: sqlite3.Connection, repo: str) -> frozenset[str]:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_drafts'").fetchone():
        return frozenset()
    return frozenset(str(row[0]) for row in connection.execute(
        "SELECT plan_id FROM execplan_drafts WHERE repo = ? AND pending = 1", (repo,),
    ))


def evidence(connection: sqlite3.Connection, repo: str, plan_id: str) -> dict | None:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_drafts'").fetchone():
        return None
    row = connection.execute("SELECT evidence FROM execplan_drafts WHERE repo = ? AND plan_id = ?",
                             (repo, plan_id)).fetchone()
    return json.loads(row[0]) if row else None


def user_question_ids(connection: sqlite3.Connection, repo: str) -> frozenset[str]:
    return frozenset(plan_id for plan_id in pending_ids(connection, repo)
                     if (saved := evidence(connection, repo, plan_id))
                     and saved.get("pending") and saved.get("question") and not saved.get("reassessment"))


def semantic_review_identity(saved: Mapping) -> str | None:
    changes = {path: change for path, change in saved.get("user_changes", {}).items()
               if change.get("verified") and change.get("before") != change.get("after")}
    if not saved.get("reassessment") and not changes:
        return None
    return execplan_drafts.content_digest(json.dumps(
        {"revision": saved.get("revision"), "decision": saved.get("decision"), "user_changes": changes},
        ensure_ascii=False, sort_keys=True).encode())


def needs_semantic_review(connection: sqlite3.Connection, repo: str, entries) -> bool:
    for plan_id in pending_ids(connection, repo):
        saved = evidence(connection, repo, plan_id) or {}
        identity = semantic_review_identity(saved)
        if saved.get("path") in entries and identity and identity != saved.get("reviewed_identity"):
            return True
    return False


def record_semantic_review(connection: sqlite3.Connection, repo: str) -> None:
    """Only successful general planning consumes this review input, never its hold."""
    for plan_id in pending_ids(connection, repo):
        saved = evidence(connection, repo, plan_id)
        identity = semantic_review_identity(saved)
        if identity:
            saved["reviewed_identity"] = identity
            connection.execute("UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id=?",
                               (json.dumps(saved, ensure_ascii=False, sort_keys=True), repo, plan_id))


def prompt_evidence(connection: sqlite3.Connection, repo: str, plan_id: str, *,
                    include_changes: bool = True) -> dict:
    saved = evidence(connection, repo, plan_id) or {}
    payload = {key: saved[key] for key in ("question", "path", "revision", "url", "reassessment", "resolutions") if key in saved}
    if "resolutions" in payload:
        payload["resolutions"] = [item["assessment"] for item in payload["resolutions"]]
    if include_changes and "user_changes" in saved:
        # Keep full before/after only in the decision audit, not every Planner input.
        payload["user_changes"] = {
            path: {**{key: value for key, value in change.items() if key not in {"before", "after"}},
                   "diff": "\n".join(difflib.unified_diff(
                       change["before"].splitlines(), change["after"].splitlines(),
                       fromfile="before/" + path, tofile="after/" + path, lineterm=""))}
            for path, change in saved["user_changes"].items()
        }
    return payload


def cancel(connection: sqlite3.Connection, repo: str, plan_id: str, revision: str) -> None:
    """Retain decision history when a proven remote deletion cancels the plan."""
    saved = evidence(connection, repo, plan_id)
    if saved is None:
        return
    saved.update(pending=False, cancelled_revision=revision)
    connection.execute(
        "UPDATE execplan_drafts SET pending=0, evidence=? WHERE repo=? AND plan_id=?",
        (json.dumps(saved, ensure_ascii=False, sort_keys=True), repo, plan_id),
    )


def validate_questions(connection: sqlite3.Connection, repo: str,
                       catalog: Mapping[str, Mapping[str, object]]) -> None:
    """A remote removal of the question cannot enable Agent duplicate drafts."""
    observed = dict(catalog)
    for plan_id in pending_ids(connection, repo):
        saved = evidence(connection, repo, plan_id)
        if saved and saved.get("identity"):
            # Keep the hold's identity even if the current document omitted it.
            observed.setdefault(plan_id, {})
            if not execplan_drafts.question_identity(observed[plan_id]):
                observed[plan_id] = {**observed[plan_id], "draft_question_identity": saved["identity"]}
    execplan_drafts.validate_questions(observed)


def observe(connection: sqlite3.Connection, repo: str, path: str, plan: Mapping[str, object],
            content_hash: str, revision: str, *, inputs: Mapping | None = None,
            remote_url: str | None = None, agent: bool = False,
            user_changes: Mapping | None = None) -> None:
    """Called inside the catalog/publication transaction, after ownership checks."""
    plan_id = str(plan["id"])
    previous = evidence(connection, repo, plan_id)
    if not previous and not plan.get("draft"):
        return
    relevant = set(plan["sources"]) | {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    basis = {"scope": plan.get("draft_scope"),
             "inputs": {key: value for key, value in (inputs or {}).items() if key in relevant}}
    cleaned = bool(previous and not plan.get("draft") and not plan.get("draft_question")
                   and not plan.get("draft_decision") and (not previous.get("pending") or previous.get("reassessment")))
    if cleaned and previous["basis"] == basis:
        return  # 양식 정리·동일 입력 재관측은 새 승인이 아니다.
    question = str(plan.get("draft_question") or (previous or {}).get("question", ""))
    # An absent question is not a decision, and cannot erase the saved question.
    unchanged = bool(previous and previous["basis"] == basis and plan.get("draft_question"))
    if (previous and not previous.get("pending") and unchanged and not plan.get("draft")
            and plan.get("draft_decision", {}) == previous.get("decision", {})):
        return  # 이전 버전에서 이미 확정한 동일 결정을 다시 승인 대기로 만들지 않는다.
    if agent and previous and not plan.get("draft") and not cleaned and (previous["pending"] or not unchanged):
        raise RuntimeError("Agent는 게시된 draft의 사용자 판단 대기를 해제할 수 없습니다.")
    # Include the inputs and their observation revision, even when plan bytes
    # did not change. A basis that changes and later returns also needs consent.
    held_revision = previous["revision"] if unchanged else execplan_drafts.content_digest(
        json.dumps({"basis": basis, "content_hash": content_hash, "revision": revision},
                   ensure_ascii=False, sort_keys=True).encode("utf-8"))
    decision = dict(plan.get("draft_decision", {}))
    changes = dict(user_changes or {})
    retained_decision = bool(previous and unchanged and previous.get("decision_ready")
                             and decision == previous.get("decision"))
    if retained_decision:
        changes = {**previous.get("user_changes", {}), **changes}
    origin_verified = bool(changes.get(path, {}).get("verified") or retained_decision)
    approved = bool(not agent and origin_verified and unchanged
                    and decision.get("Decision") == "approve"
                    and (decision.get("Revision", held_revision) == held_revision
                         or previous.get("decision_ready") and decision == previous.get("decision"))
                    and decision.get("Answer"))
    decisions = list((previous or {}).get("decisions", ()))
    if not agent and decision:
        audit = {"value": decision, "basis": basis, "revision": content_hash, "accepted": approved}
        if audit not in decisions:
            decisions.append(audit)
    question_revision = previous["remote_revision"] if unchanged and previous["path"] == path else revision
    record = {"question": question, "basis": basis, "revision": held_revision,
              "identity": execplan_drafts.question_identity(plan) or (previous or {}).get("identity"),
              "path": path, "remote_revision": question_revision, "pending": True,
              "decision": decision if not agent else (previous or {}).get("decision", {}),
              "decisions": decisions, "decision_ready": approved, "user_changes": changes}
    if cleaned:
        record.update(question="", identity=None, reassessment=True)
    if previous and unchanged and previous.get("prepared_resolution"):
        record["prepared_resolution"] = previous["prepared_resolution"]
    if previous and previous.get("resolutions"):
        record["resolutions"] = previous["resolutions"]
    if previous and previous.get("reviewed_identity"):
        record["reviewed_identity"] = previous["reviewed_identity"]
    origin = remote_url or (previous or {}).get("remote_url")
    if origin:
        record["remote_url"] = origin
        record["url"] = origin.removesuffix(".git").rstrip("/") + "/blob/" + question_revision + "/" + quote(path, safe="/")
    elif previous and previous.get("url"):
        record["url"] = previous["url"]
    connection.execute("""
        INSERT INTO execplan_drafts VALUES (?, ?, ?, ?)
        ON CONFLICT(repo, plan_id) DO UPDATE SET pending=excluded.pending, evidence=excluded.evidence
    """, (repo, plan_id, 1, json.dumps(record, ensure_ascii=False, sort_keys=True)))


def prepare_resolution(connection, repo, plan_id, resolution):
    """문서 정리 전 복구 근거만 저장하며 실행 gate는 유지한다."""
    from .runtime_state import _transaction
    with _transaction(connection):
        saved = evidence(connection, repo, plan_id)
        if not saved or saved["revision"] != resolution["revision"] or saved["basis"] != resolution["basis"]:
            raise RuntimeError("draft 질문 정리 전에 판단 근거가 변경됐습니다.")
        saved["prepared_resolution"] = resolution
        connection.execute("UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id=?",
                           (json.dumps(saved, ensure_ascii=False, sort_keys=True), repo, plan_id))


def resolve(connection, repo, path, plan, content_hash, revision, resolution, *, inputs=None):
    """게시 확인 뒤 같은 transaction에서 대기·감사·정리 결과를 확정한다."""
    saved = evidence(connection, repo, str(plan["id"]))
    if (not saved or saved.get("prepared_resolution") != resolution
            or saved["revision"] != resolution["revision"] or saved["basis"] != resolution["basis"]
            or resolution["content_hash"] != content_hash
            or any((inputs or {}).get(key) != value for key, value in saved["basis"]["inputs"].items())):
        raise RuntimeError("draft 질문 해소의 게시·현재 입력 근거가 변경됐습니다.")
    history = list(saved.get("resolutions", []))
    audit = {**resolution, "publication_revision": revision, "content_hash": content_hash}
    if audit not in history:
        history.append(audit)
    pending = bool(plan.get("draft"))
    relevant = set(plan["sources"]) | {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    saved.update(path=path, remote_revision=revision, pending=pending,
                 question=plan.get("draft_question", ""),
                 identity=execplan_drafts.question_identity(plan), resolutions=history,
                 basis={"scope": plan.get("draft_scope"), "inputs": {
                     key: value for key, value in (inputs or {}).items() if key in relevant}},
                 decision=plan.get("draft_decision", {}) if pending else {},
                 decision_ready=bool(pending and plan.get("draft_decision") and saved.get("decision_ready")),
                 user_changes=saved.get("user_changes", {}) if pending else {})
    saved.pop("prepared_resolution", None)
    saved.pop("reassessment", None)
    saved["revision"] = execplan_drafts.content_digest(json.dumps(saved["basis"], sort_keys=True).encode())
    if saved.get("remote_url"):
        saved["url"] = saved["remote_url"].removesuffix(".git").rstrip("/") + "/blob/" + revision + "/" + quote(path, safe="/")
    connection.execute("UPDATE execplan_drafts SET pending=?, evidence=? WHERE repo=? AND plan_id=?",
                       (int(pending), json.dumps(saved, ensure_ascii=False, sort_keys=True), repo, str(plan["id"])))
