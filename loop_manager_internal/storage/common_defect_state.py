"""Append-only adoption of evidence-bound common correction goals."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import re


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS common_defect_links (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL,
        result_id INTEGER NOT NULL REFERENCES validation_results(id),
        goal_key TEXT NOT NULL, correction_plan_id TEXT NOT NULL,
        reviewed_revision TEXT NOT NULL, confirmed_revision TEXT NOT NULL,
        evidence TEXT NOT NULL,
        UNIQUE(repo,result_id,goal_key,confirmed_revision)
    )""")
    connection.execute('CREATE INDEX IF NOT EXISTS common_defect_links_result ON common_defect_links(repo,result_id,id)')
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS common_defect_links_{action.lower()}_immutable "
                           f"BEFORE {action} ON common_defect_links BEGIN "
                           "SELECT RAISE(ABORT, 'common defect audit is immutable'); END")
    connection.execute("""CREATE TABLE IF NOT EXISTS common_defect_merges (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL,
        pr_url TEXT NOT NULL, head TEXT NOT NULL, merge_oid TEXT NOT NULL,
        verification TEXT NOT NULL,
        UNIQUE(repo,plan_id,pr_url,head,merge_oid,verification)
    )""")
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS common_defect_merges_{action.lower()}_immutable "
                           f"BEFORE {action} ON common_defect_merges BEGIN "
                           "SELECT RAISE(ABORT, 'common defect audit is immutable'); END")


def _verification(connection, plan, head):
    from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
    from loop_manager_internal.execution.result_validation import parse_reviewer_result, ParseReviewerResultEffects, ReviewerProtocolError
    from loop_manager_internal.storage import verify_receipts
    from loop_manager_internal.verification.inputs import InputObservation
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'verify_receipts', 'verify_receipt_cycles'} <= tables:
        return None
    try:
        definition = DefinitionSnapshot.deserialize(plan['definition_snapshot'])
        base = plan['validated_base_sha']
        if (definition is None or not base or not isinstance(head, str)
                or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head) is None or plan['pr_head_oid'] != head
                or not isinstance(plan['review_result'], str) or not plan['verify_result']
                or plan['verify_token'] != definition.verify_token(base, head, plan['verify_result'])
                or plan['review_token'] != definition.review_token(base, head)
                or parse_reviewer_result(ParseReviewerResultEffects(lambda _: ()), plan['review_result'])[0] != 'PASS'):
            return None
        latest = verify_receipts.latest_decision(connection, verify_receipts.selected(connection, plan['repo'], plan['plan_id']))
        if latest is None:
            return None
        observed = InputObservation(**json.loads(latest['observation']))
        evidence = verify_receipts.candidate_evidence(connection, plan['repo'], plan['plan_id'],
                                                    head, plan['verify_result'], observed)
        if evidence is None or evidence['kind'] == 'waiver':
            return None
        return dict(receipt_id=evidence['receipt']['id'], kind=evidence['kind'],
                    origin_id=evidence.get('origin', evidence['receipt'])['id'],
                    verify_token=plan['verify_token'], review_token=plan['review_token'],
                    definition_sha256=hashlib.sha256(plan['definition_snapshot'].encode()).hexdigest(), base=base)
    except (KeyError, IndexError, TypeError, ValueError, ReviewerProtocolError):
        return None


def record_merge(connection, repo, plan_id, pr_url, head, merge_oid):
    """Called only by Manager after observing this PR's actual Merge."""
    ensure_schema(connection)
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
    merge_oid = merge_oid if isinstance(merge_oid, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', merge_oid) else ''
    verification = _verification(connection, plan, head) if plan is not None and plan['pr_url'] == pr_url else None
    connection.execute('INSERT OR IGNORE INTO common_defect_merges '
        '(repo,plan_id,pr_url,head,merge_oid,verification) VALUES (?,?,?,?,?,?)',
        (repo, plan_id, pr_url, head or '', merge_oid or '', json.dumps(verification, sort_keys=True)))


def reassessment(connection, repo, plan_id):
    """Return current failure links and native Merge/validation evidence, never success for the affected task."""
    from loop_manager_internal.execution.validation_feedback import redact_secrets
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'common_defect_links', 'common_defect_merges', 'validation_results',
            'common_defect_reports', 'validation_feedback_current', 'validation_feedback_events'} <= tables:
        return None
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
    if plan is None or 'worker_result' not in plan.keys():
        return None
    reports = set()
    if plan['worker_result']:
        row = connection.execute("SELECT r.id FROM validation_results r JOIN common_defect_reports c ON c.result_id=r.id "
            "WHERE r.repo=? AND r.plan_id=? AND r.stage='worker' AND r.raw_output=? AND c.report IS NOT NULL "
            "ORDER BY r.id DESC LIMIT 1", (repo, plan_id, redact_secrets(plan['worker_result']))).fetchone()
        if row is not None:
            reports.add(row[0])
    reports.update(row[0] for row in connection.execute(
        "SELECT e.result_id FROM validation_feedback_current c JOIN validation_feedback_events e ON e.sequence=c.sequence "
        "JOIN common_defect_reports r ON r.result_id=e.result_id "
        "WHERE c.repo=? AND c.plan_id=? AND c.active=1 AND r.report IS NOT NULL", (repo, plan_id)))
    # Rejected protocol results have an immutable audit but no active feedback
    # event. Only the latest result of the actual blocked stage can supply one.
    stage = ('review' if plan['blocked_reason'] in {'review_protocol_exhausted', 'review_retry_exhausted'}
             else 'verify' if plan['blocked_reason'] in {'verify_retry_exhausted', 'verify_timeout_limit_insufficient'} else None)
    if stage:
        latest = connection.execute('SELECT r.id,r.classification,c.report FROM validation_results r '
            'LEFT JOIN common_defect_reports c ON c.result_id=r.id WHERE r.repo=? AND r.plan_id=? AND r.stage=? '
            'ORDER BY r.id DESC LIMIT 1', (repo, plan_id, stage)).fetchone()
        if latest is not None and latest['classification'] != 'success' and latest['report'] is not None:
            reports.add(latest['id'])
    if not reports:
        return None
    current = {}
    for row in connection.execute('SELECT l.* FROM common_defect_links l JOIN validation_results r ON r.id=l.result_id '
                                 'WHERE l.repo=? AND r.plan_id=? ORDER BY l.id', (repo, plan_id)):
        if row['result_id'] in reports:
            current[(row['result_id'], row['goal_key'])] = row
    if not current:
        return None
    corrections = []
    for row in current.values():
        target = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                    (repo, row['correction_plan_id'])).fetchone()
        merge = connection.execute("SELECT * FROM common_defect_merges WHERE repo=? AND plan_id=? "
            "AND merge_oid!='' AND verification!='null' ORDER BY id DESC LIMIT 1",
            (repo, row['correction_plan_id'])).fetchone()
        if merge is not None and target is not None and (target['status'] != 'MERGED'
                or target['pr_url'] != merge['pr_url'] or target['pr_head_oid'] != merge['head']):
            merge = None
        evidence = json.loads(row['evidence'])
        corrections.append(dict(result_id=row['result_id'], goal_key=row['goal_key'],
            plan_id=row['correction_plan_id'], source=evidence['source'], quote=evidence['quote'],
            cause=evidence['cause'], completion=evidence['completion'],
            confirmed_revision=row['confirmed_revision'], merge=dict(merge) if merge is not None else None))
    return dict(ready=all(item['merge'] is not None for item in corrections)
                and reports <= {item['result_id'] for item in corrections}, corrections=corrections)


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
