"""Append-only adoption of evidence-bound common correction goals."""
from __future__ import annotations

import hashlib
import json
import sqlite3


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS common_defect_links (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL,
        result_id INTEGER NOT NULL REFERENCES validation_results(id),
        goal_key TEXT NOT NULL, correction_plan_id TEXT NOT NULL,
        reviewed_revision TEXT NOT NULL, confirmed_revision TEXT NOT NULL,
        evidence TEXT NOT NULL,
        UNIQUE(repo,result_id,goal_key,confirmed_revision)
    )""")
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS common_defect_links_{action.lower()}_immutable "
                           f"BEFORE {action} ON common_defect_links BEGIN "
                           "SELECT RAISE(ABORT, 'common defect audit is immutable'); END")


def goal_key(link: dict) -> str:
    return hashlib.sha256(json.dumps([link[key] for key in ("source", "cause", "completion")],
                                     ensure_ascii=True).encode()).hexdigest()


def links(connection: sqlite3.Connection, repo: str, result_id: int) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT correction_plan_id,goal_key,reviewed_revision,confirmed_revision,evidence "
        "FROM common_defect_links WHERE repo=? AND result_id=? ORDER BY id", (repo, result_id))]


def check_reuse(connection: sqlite3.Connection, repo: str, proposals: list[dict]) -> None:
    """An explicit goal identity cannot acquire two live correction owners."""
    owners = {}
    for row in connection.execute(
        "SELECT l.goal_key,l.correction_plan_id,p.sources,l.evidence FROM common_defect_links l "
        "JOIN exec_plans p ON p.repo=l.repo AND p.plan_id=l.correction_plan_id "
        "WHERE l.repo=? AND p.status!='MERGED' ORDER BY l.id", (repo,)):
        evidence = json.loads(row["evidence"])
        if evidence["source"] in json.loads(row["sources"]):
            owners[row["goal_key"]] = row["correction_plan_id"]
    for link in proposals:
        key = goal_key(link)
        if key in owners and owners[key] != link["plan_id"]:
            raise ValueError("공통 교정 목표를 맡은 유효 계획을 재사용해야 합니다: " + str(owners[key]))
        owners[key] = link["plan_id"]


def confirm(connection: sqlite3.Connection, repo: str, reviewed_revision: str,
            confirmed_revision: str, proposals: list[dict]) -> None:
    """Caller owns the remote-confirmation/catalog transaction; no external I/O."""
    if not proposals:
        return
    check_reuse(connection, repo, proposals)
    for link in proposals:
        target = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                                    (repo, link["plan_id"])).fetchone()
        expected_revision = link["target_revision"] or confirmed_revision
        if (target is None or target["path"] != link["target_path"]
                or (target["current_revision_sha"] or target["plan_sha"]) != expected_revision
                or link["source"] not in json.loads(target["sources"])):
            raise ValueError("공통 교정 계획의 게시 identity가 변경됐습니다.")
        evidence = json.dumps(link, ensure_ascii=True, sort_keys=True)
        for result_id in link["reports"]:
            report = connection.execute(
                "SELECT c.report FROM common_defect_reports c JOIN validation_results r ON r.id=c.result_id "
                "WHERE r.repo=? AND r.id=?", (repo, result_id)).fetchone()
            if report is None or report["report"] is None:
                raise ValueError("공통 결함 보고의 원본 연결을 확인할 수 없습니다.")
            previous = connection.execute(
                "SELECT correction_plan_id,reviewed_revision,evidence FROM common_defect_links "
                "WHERE repo=? AND result_id=? AND goal_key=? AND confirmed_revision=?",
                (repo, result_id, goal_key(link), confirmed_revision)).fetchone()
            expected = (link["plan_id"], reviewed_revision, evidence)
            if previous is not None:
                if tuple(previous) != expected:
                    raise ValueError("동일 게시의 공통 교정 연결 근거가 다릅니다.")
                continue
            connection.execute(
                "INSERT INTO common_defect_links "
                "(repo,result_id,goal_key,correction_plan_id,reviewed_revision,confirmed_revision,evidence) "
                "VALUES (?,?,?,?,?,?,?)",
                (repo, result_id, goal_key(link), link["plan_id"], reviewed_revision, confirmed_revision, evidence))
