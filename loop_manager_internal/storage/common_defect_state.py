"""Append-only adoption of evidence-bound common correction goals."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import re


def assert_plan_erasure_available(connection: sqlite3.Connection, repo: str, plan_id: str) -> None:
    """Keep live independent correction evidence until its own relation is resolved."""
    from .task_calls import CallDeferred
    from .execplan_catalog_state import is_cancelled
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'common_defect_merges' in tables and connection.execute(
        'SELECT 1 FROM common_defect_merges WHERE repo=? AND plan_id=?', (repo, plan_id),
    ).fetchone():
        raise CallDeferred('확인된 공통 교정 Merge 근거는 삭제할 수 없습니다.')
    if 'common_defect_links' not in tables:
        return
    if 'validation_results' not in tables:
        raise CallDeferred('공통 결함 연결의 원본 저장소를 확인할 수 없습니다.')
    for link in connection.execute(
        'SELECT l.*,r.repo AS report_repo,r.plan_id AS report_plan FROM common_defect_links l '
        'LEFT JOIN validation_results r ON r.id=l.result_id '
        'WHERE (l.repo=? AND l.correction_plan_id=?) OR (r.repo=? AND r.plan_id=?)',
        (repo, plan_id, repo, plan_id),
    ).fetchall():
        if link['report_repo'] != link['repo']:
            raise CallDeferred('공통 결함 연결의 repository 귀속을 확인할 수 없습니다.')
        if connection.execute('SELECT 1 FROM common_defect_links WHERE repo=? AND result_id=? AND goal_key=? AND id>?',
                              (repo, link['result_id'], link['goal_key'], link['id'])).fetchone():
            continue  # An adopted successor owns this result/goal now.
        independent = link['report_plan'] if link['correction_plan_id'] == plan_id else link['correction_plan_id']
        if independent == plan_id or is_cancelled(connection, repo, independent):
            continue
        row = connection.execute('SELECT status FROM exec_plans WHERE repo=? AND plan_id=?', (repo, independent)).fetchone()
        merged = row is not None and row['status'] == 'MERGED'
        if not merged and 'common_defect_merges' in tables:
            merged = connection.execute('SELECT 1 FROM common_defect_merges WHERE repo=? AND plan_id=?',
                                        (repo, independent)).fetchone() is not None
        if not merged:
            raise CallDeferred('독립 작업의 공통 결함 근거가 삭제 대상 연결을 아직 필요로 합니다.')


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


def _individual_verification(connection, plan, head):
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


def _verification(connection, plan, head):
    from loop_manager_internal.application.contracts import VerifyOutput
    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'manual_work_groups' not in tables:
        return _individual_verification(connection, plan, head)
    store = ManualWorkRepository(connection)
    group = store.for_plan(plan['repo'], plan['plan_id'])
    if group is None:
        return _individual_verification(connection, plan, head)
    try:
        store.workspace_owner(plan['repo'], plan['plan_id'])
        publication = connection.execute('SELECT * FROM manual_work_publications WHERE repo=? AND group_id=?',
                                         (plan['repo'], group['group_id'])).fetchone()
        if (publication is None or group['pending_source_head']
                or publication['request_id'] != group['request_id'] or not publication['request_id']
                or publication['head'] != head or publication['pr_url'] != plan['pr_url']
                or plan['pr_head_oid'] != head or publication['base_sha'] != plan['validated_base_sha']):
            return None
        members = json.loads(group['members'])
        evidence = json.loads(publication['evidence'])
        required = {(member, stage) for member in members for stage in ('verify', 'review')}
        if required != {(row['plan_id'], row['stage']) for row in evidence}:
            return None
        verify_outputs = set()
        for row in evidence:
            if row['result'] != 'PASS':
                return None
            receipt = connection.execute('SELECT * FROM manual_work_results WHERE repo=? AND group_id=? '
                'AND plan_id=? AND stage=? AND head=? AND base_sha=? AND input_token=? AND result=? AND request_id=?',
                (plan['repo'], group['group_id'], row['plan_id'], row['stage'], head,
                 publication['base_sha'], row['input_token'], 'PASS', row['request_id'])).fetchone()
            if receipt is None:
                return None
            if row['stage'] == 'verify':
                verify_outputs.add(receipt['detail'])
            if row['stage'] == 'review':
                from loop_manager_internal.execution.result_validation import parse_reviewer_result, ParseReviewerResultEffects
                if (parse_reviewer_result(ParseReviewerResultEffects(lambda _: ()), receipt['detail'])[0] not in {'PASS', 'FAIL'}
                        or [result for member, result in re.findall(
                            r'(?m)^MANUAL_PLAN_RESULT: (G-\d{3,}) (PASS|FAIL)\s*$', receipt['detail'])
                            if member == row['plan_id']] != ['PASS']):
                    return None
        # One native full run covers the shared candidate; every member still
        # needs its own explicit Review PASS in the confirmed publication.
        plans = [connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                    (plan['repo'], member)).fetchone() for member in members]
        for selected in plans:
            if (selected is None or selected['pr_url'] != publication['pr_url']
                    or selected['pr_head_oid'] != head or selected['validated_base_sha'] != publication['base_sha']):
                return None
        for selected in plans:
            proof = _individual_verification(connection, selected, head)
            if proof is not None and all(output == selected['verify_result']
                    or str(VerifyOutput(output)) == selected['verify_result'] for output in verify_outputs):
                return dict(proof, group_id=group['group_id'], member_id=plan['plan_id'],
                            execution_plan_id=selected['plan_id'], publication_sha256=digest(dict(publication)))
    except (KeyError, IndexError, TypeError, ValueError, RuntimeError):
        return None
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


def _current_reports(connection, repo, plan_id):
    """Select immutable failures bound to the current result or the same frozen block."""
    from loop_manager_internal.execution.validation_feedback import redact_secrets
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'common_defect_links', 'common_defect_merges', 'validation_results',
            'common_defect_reports', 'validation_feedback_current', 'validation_feedback_events'} <= tables:
        return set()
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
    if plan is None or 'worker_result' not in plan.keys():
        return set()
    worker_result = plan['worker_result']
    if (not worker_result and plan['blocked_reason'] == 'common_defect_pending'
            and {'blocked_plan_reconciliations', 'execplan_blocks'} <= tables):
        saved = connection.execute('SELECT context FROM blocked_plan_reconciliations '
                                   'WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
        block = connection.execute('SELECT * FROM execplan_blocks WHERE repo=? AND plan_id=?',
                                   (repo, plan_id)).fetchone()
        try:
            context = json.loads(saved['context']) if saved else {}
            # Rebase invalidates candidate results, not the original failure.
            # Only the same still-blocked cause may borrow its frozen report.
            if (block is not None and context.get('block') == dict(block)
                    and context.get('identity', {}).get('path') == plan['path']):
                worker_result = context.get('artifacts', {}).get('worker_result')
        except (TypeError, ValueError, AttributeError):
            pass
    reports = set()
    if isinstance(worker_result, str) and worker_result:
        row = connection.execute("SELECT r.id FROM validation_results r JOIN common_defect_reports c ON c.result_id=r.id "
            "WHERE r.repo=? AND r.plan_id=? AND r.stage='worker' AND r.raw_output=? AND c.report IS NOT NULL "
            "ORDER BY r.id DESC LIMIT 1", (repo, plan_id, redact_secrets(worker_result))).fetchone()
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
    return reports


def reassessment(connection, repo, plan_id):
    """Return current failure links and native Merge/validation evidence, never success for the affected task."""
    reports = _current_reports(connection, repo, plan_id)
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
    owner_execution = True
    owns_correction = False
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
        report = connection.execute('SELECT definition_revision FROM validation_results WHERE id=?',
                                    (row['result_id'],)).fetchone()
        owner = (target is not None and target['plan_id'] == plan_id and target['status'] == 'PLAN_BLOCKED'
                 and target['path'] == evidence['target_path'] and bool(report['definition_revision'])
                 and (target['current_revision_sha'] or target['plan_sha']) ==
                     (evidence['target_revision'] or row['confirmed_revision'])
                 and (target['current_revision_sha'] or target['plan_sha']) != report['definition_revision']
                 and evidence['source'] in json.loads(target['sources']))
        owner_execution = owner_execution and (owner or merge is not None)
        owns_correction = owns_correction or owner
        corrections.append(dict(result_id=row['result_id'], goal_key=row['goal_key'],
            plan_id=row['correction_plan_id'], source=evidence['source'], quote=evidence['quote'],
            cause=evidence['cause'], completion=evidence['completion'],
            confirmed_revision=row['confirmed_revision'], merge=dict(merge) if merge is not None else None))
    covered = reports <= {item['result_id'] for item in corrections}
    return dict(ready=all(item['merge'] is not None for item in corrections) and covered,
                owner_execution=owner_execution and owns_correction and covered, corrections=corrections)


def unlinked_reports(connection, repo, plan_id):
    """Current immutable reports are assessment input, never correction/validation proof."""
    reports = _current_reports(connection, repo, plan_id)
    rows = connection.execute("SELECT r.id,r.definition_revision,c.report FROM validation_results r "
        "JOIN common_defect_reports c ON c.result_id=r.id WHERE r.repo=? AND r.plan_id=? "
        "AND c.report IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM common_defect_links l WHERE l.repo=r.repo AND l.result_id=r.id) ORDER BY r.id",
        (repo, plan_id)).fetchall() if reports else []
    return [dict(id=row['id'], revision=row['definition_revision'], report=json.loads(row['report']))
            for row in rows if row['id'] in reports]


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
