"""Durable decision holds, independent of execution phases and document drafts."""
from __future__ import annotations

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


def prompt_evidence(connection: sqlite3.Connection, repo: str, plan_id: str) -> dict:
    saved = evidence(connection, repo, plan_id) or {}
    return {key: saved[key] for key in ("question", "path", "revision", "url") if key in saved}


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
            remote_url: str | None = None, agent: bool = False) -> None:
    """Called inside the catalog/publication transaction, after ownership checks."""
    plan_id = str(plan["id"])
    previous = evidence(connection, repo, plan_id)
    if not previous and not plan.get("draft"):
        return
    relevant = set(plan["sources"]) | {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    basis = {"scope": plan.get("draft_scope"),
             "inputs": {key: value for key, value in (inputs or {}).items() if key in relevant}}
    question = str(plan.get("draft_question") or (previous or {}).get("question", ""))
    # An absent question is not a decision, and cannot erase the saved question.
    unchanged = bool(previous and previous["basis"] == basis and plan.get("draft_question"))
    if agent and previous and not plan.get("draft") and (previous["pending"] or not unchanged):
        raise RuntimeError("Agent는 게시된 draft의 사용자 판단 대기를 해제할 수 없습니다.")
    held_revision = previous["revision"] if unchanged else content_hash
    decision = dict(plan.get("draft_decision", {}))
    approved = bool(not agent and not plan.get("draft") and unchanged
                    and decision.get("Decision") == "approve"
                    and decision.get("Revision") == held_revision and decision.get("Answer"))
    if previous and not previous.get("pending") and unchanged and not plan.get("draft"):
        approved = approved or decision == previous.get("decision")
    decisions = list((previous or {}).get("decisions", ()))
    if not agent and decision:
        audit = {"value": decision, "basis": basis, "revision": content_hash, "accepted": approved}
        if audit not in decisions:
            decisions.append(audit)
    question_revision = previous["remote_revision"] if unchanged and previous["path"] == path else revision
    record = {"question": question, "basis": basis, "revision": held_revision,
              "identity": execplan_drafts.question_identity(plan) or (previous or {}).get("identity"),
              "path": path, "remote_revision": question_revision, "pending": not approved,
              "decision": decision if not agent else (previous or {}).get("decision", {}),
              "decisions": decisions}
    origin = remote_url or (previous or {}).get("remote_url")
    if origin:
        record["remote_url"] = origin
        record["url"] = origin.removesuffix(".git").rstrip("/") + "/blob/" + question_revision + "/" + quote(path, safe="/")
    elif previous and previous.get("url"):
        record["url"] = previous["url"]
    connection.execute("""
        INSERT INTO execplan_drafts VALUES (?, ?, ?, ?)
        ON CONFLICT(repo, plan_id) DO UPDATE SET pending=excluded.pending, evidence=excluded.evidence
    """, (repo, plan_id, int(not approved), json.dumps(record, ensure_ascii=False, sort_keys=True)))
