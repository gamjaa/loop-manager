"""Deterministic maintenance selection and durable module-level model receipts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.maintenance.maintenance_file_inventory import cached_source_inventory, root_placement_inventory

RESULT_PREFIX = "MODULE_REVIEW_RESULT: "
MAX_UNIT_BYTES = 160_000
MAX_MODULES = 8
MAX_OVERVIEW_SYMBOLS = 256
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiered_maintenance_success (
    repo TEXT PRIMARY KEY, records_json TEXT NOT NULL, roots_json TEXT NOT NULL,
    source_revision TEXT NOT NULL, full_merged_count INTEGER, full_source_revision TEXT,
    assessment_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tiered_maintenance_assessments (
    repo TEXT PRIMARY KEY, cycle INTEGER NOT NULL, source_revision TEXT NOT NULL,
    digest TEXT NOT NULL, assessment_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tiered_maintenance_units (
    repo TEXT NOT NULL, digest TEXT NOT NULL, session_id TEXT, result_json TEXT,
    error TEXT, PRIMARY KEY (repo, digest));
CREATE TABLE IF NOT EXISTS tiered_maintenance_requests (
    repo TEXT PRIMARY KEY, request_id TEXT NOT NULL);
"""


class TieredReviewError(ValueError):
    pass


def ensure_schema(connection):
    # executescript would commit an outer publication transaction.
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def digest(value) -> str:
    return hashlib.sha256(code_index.encode(value).encode()).hexdigest()


def _cycles(records) -> set[tuple[str, ...]]:
    graph = {path: {ref["path"] for ref in item["facts"]["references"]} for path, item in records.items()}
    seen, order, reverse = set(), [], {path: set() for path in graph}
    for path, targets in graph.items():
        for target in targets:
            if target in reverse:
                reverse[target].add(path)
    for path in sorted(graph):
        stack = [(path, False)]
        while stack:
            current, closing = stack.pop()
            if closing:
                order.append(current)
            elif current not in seen:
                seen.add(current)
                stack.append((current, True))
                stack.extend((target, False) for target in sorted(graph[current], reverse=True) if target in graph and target not in seen)
    seen, found = set(), set()
    for path in reversed(order):
        if path in seen:
            continue
        stack, component = [path], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.add(current)
            stack.extend(reverse[current] - seen)
        if len(component) > 1 or path in graph[path]:
            found.add(tuple(sorted(component)))
    return found


def _duplicates(records):
    groups = {}
    for path, item in records.items():
        for duplicate in item["facts"]["duplicates"]:
            groups.setdefault(duplicate["hash"], set()).add((path, duplicate["symbol"]))
    return {tuple(sorted(items)) for items in groups.values() if len(items) > 1}


def _model_record(record):
    """Transport an overview; full parser facts remain in the reusable index."""
    facts = record["facts"]
    return {"content_hash": record["content_hash"], "mode": record["mode"], "facts": {
        "symbols": [[item["name"][:256], item["line"], item["end_line"]] for item in facts["symbols"][:MAX_OVERVIEW_SYMBOLS]],
        "symbol_count": len(facts["symbols"]), "symbols_truncated": len(facts["symbols"]) > MAX_OVERVIEW_SYMBOLS
            or any(len(item["name"]) > 256 for item in facts["symbols"]),
        "references": sorted({item["path"] for item in facts["references"]}),
        "dependent_modules": sorted({code_index.module_group(path) for path in facts["dependents"]}),
        "related_tests": facts["related_tests"], "uncertainties": facts["uncertainties"],
        "boundary": facts["boundary"], "duplicates": facts["duplicates"],
        "contracts_digest": digest(facts["contracts"]), "facts_digest": digest(facts)}}


@dataclass(frozen=True)
class Selection:
    required: bool
    mode: str
    reasons: tuple[str, ...]
    paths: tuple[str, ...]
    index: code_index.CodeIndex
    roots: tuple[dict, ...]
    merged_count: int
    request_id: str | None = None


class TieredReviewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def success(self, repo):
        row = self.connection.execute("SELECT records_json, roots_json, source_revision, full_merged_count, "
            "full_source_revision, assessment_digest FROM tiered_maintenance_success WHERE repo = ?", (repo,)).fetchone()
        return None if row is None else {"records": json.loads(row[0]), "roots": json.loads(row[1]),
            "source_revision": row[2], "full_merged_count": row[3], "full_source_revision": row[4], "digest": row[5]}

    def select(self, repo, repository, base, revision, interval, merged_count, *, force_full=False):
        if interval <= 0:
            raise TieredReviewError("정밀 점검 주기는 양의 정수여야 합니다.")
        inventory = cached_source_inventory(self.connection, repository, base, revision, include_uncertain=True)
        index = code_index.build_index(self.connection, repo + ":remote", inventory,
                                      lambda source: code_index.git(repository, "show", f"{revision}:{source.path}"))
        roots = tuple({"path": root.path, "category": root.category} for root in root_placement_inventory(inventory))
        previous = self.success(repo)
        request = self.connection.execute("SELECT request_id FROM tiered_maintenance_requests WHERE repo = ?", (repo,)).fetchone()
        request_id = str(request[0]) if request else None
        count = previous["full_merged_count"] if previous else None
        if count is not None and merged_count < count:
            raise TieredReviewError("전체 정밀 Merge 기준이 현재 이력보다 큽니다.")
        if force_full or request_id or count is None or merged_count - count >= interval:
            reason = "requested_full" if force_full or request_id else "initial_full" if count is None else "periodic_full"
            return Selection(True, "full", (reason,), tuple(index.records), index, roots, merged_count, request_id)
        old = previous["records"]
        changed = {path for path in old.keys() | index.records.keys() if old.get(path) != index.records.get(path)}
        if not changed and list(roots) == previous["roots"]:
            return Selection(False, "fast", ("same_input",), (), index, roots, merged_count)
        reasons, risky = set(), set()
        for path in sorted(changed):
            before, after = old.get(path), index.records.get(path)
            if before is None or after is None or before["facts"]["contracts"] != after["facts"]["contracts"]:
                if ((before and before["facts"]["contracts"]) or (after and after["facts"]["contracts"])):
                    reasons.add("public_contract_changed")
                    risky.add(path)
            if after and after["facts"]["uncertainties"]:
                reasons.add("uncertain_relations")
                risky.add(path)
            actual_change = before is None or after is None or (
                before["facts"]["semantic_hash"], before["mode"]) != (after["facts"]["semantic_hash"], after["mode"])
            if actual_change and ((before and before["facts"]["boundary"]) or (after and after["facts"]["boundary"])):
                reasons.add("state_storage_recovery_or_permission_changed")
                risky.add(path)
        for component in _cycles(index.records) - _cycles(old):
            reasons.add("new_dependency_cycle")
            risky.update(component)
        for group in _duplicates(index.records) - _duplicates(old):
            reasons.add("new_structural_duplicate")
            risky.update(path for path, symbol in group)
        if list(roots) != previous["roots"]:
            reasons.add("root_placement_changed")
            risky.update(path for path in index.records if "/" not in path)
            if not risky:
                risky.update(index.records)
        if not reasons:
            return Selection(True, "fast", ("known_incremental_change",), tuple(sorted(changed & index.records.keys())),
                             index, roots, merged_count)
        affected = set(risky)
        for path in list(risky):
            for records in (old, index.records):
                if path in records:
                    facts = records[path]["facts"]
                    affected.update(facts["dependents"])
                    affected.update(ref["path"] for ref in facts["references"])
                    affected.update(facts["related_tests"])
        groups = {code_index.module_group(path) for path in affected}
        selected = tuple(path for path in index.records if path in affected or (
            not path.startswith(("tests/", "test/")) and code_index.module_group(path) in groups))
        # Deleted risky modules still require checking their surviving consumers.
        if not selected:
            selected = tuple(index.records)
        return Selection(True, "targeted", tuple(sorted(reasons)), selected, index, roots, merged_count)

    def prepare(self, repo, cycle, selection: Selection, *, cycle_id=None, review_mode=None,
                guidance_hash=None):
        value = {"version": 1, "mode": selection.mode, "reasons": list(selection.reasons),
            "records": selection.index.records, "roots": list(selection.roots), "paths": list(selection.paths),
            "merged_count": selection.merged_count, "request_id": selection.request_id,
            "source_revision": selection.index.revision, "cycle": cycle, "cycle_id": cycle_id,
            "review_mode": review_mode or selection.mode}
        value["guidance_hash"] = guidance_hash or common_guidance.effective_planning_hash(self.connection, repo)
        value["digest"] = digest(value)
        row = self.connection.execute("SELECT cycle, source_revision, digest, assessment_json FROM tiered_maintenance_assessments "
                                      "WHERE repo = ?", (repo,)).fetchone()
        if row and (row[0], row[1]) == (cycle, selection.index.revision):
            saved = json.loads(row[3])
            if saved["records"] != value["records"] or saved["roots"] != value["roots"]:
                raise TieredReviewError("같은 assessment identity의 코드맵이 달라졌습니다.")
            if saved.get('guidance_hash'):
                return saved
            # A legacy assessment is not evidence of the newly injected rules.
            # Keep its source/task scope and audit, but recheck its judgments.
            previous = dict(saved)
            saved['guidance_hash'] = value['guidance_hash']
            saved['prior_guidance_assessment'] = previous
            saved.pop('digest', None)
            saved['digest'] = digest(saved)
            value = saved
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO tiered_maintenance_assessments VALUES (?, ?, ?, ?, ?)",
                                    (repo, cycle, selection.index.revision, value["digest"], code_index.encode(value)))
        return value

    def active(self, repo, cycle, revision):
        row = self.connection.execute("SELECT assessment_json FROM tiered_maintenance_assessments WHERE repo = ? "
                                      "AND cycle = ? AND source_revision = ?", (repo, cycle, revision)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, repo):
        row = self.connection.execute("SELECT assessment_json FROM tiered_maintenance_assessments WHERE repo = ?", (repo,)).fetchone()
        return json.loads(row[0]) if row else None

    def units(self, assessment):
        if assessment["mode"] == "fast":
            return ()
        modules = {}
        for path in assessment["paths"]:
            modules.setdefault(code_index.module_group(path), {})[path] = _model_record(assessment["records"][path])
        parts = []
        for module, records in sorted(modules.items()):
            current = {}
            for path, item in sorted(records.items()):
                if current and len(code_index.encode({**current, path: item}).encode()) > MAX_UNIT_BYTES:
                    parts.append({"module": module, "records": current})
                    current = {}
                current[path] = item
            if current:
                parts.append({"module": module, "records": current})
        batches, batch = [], []
        for part in parts:
            if batch and (len(batch) >= MAX_MODULES or len(code_index.encode([*batch, part]).encode()) > MAX_UNIT_BYTES):
                batches.append(batch)
                batch = []
            batch.append(part)
        if batch:
            batches.append(batch)
        review_mode = assessment.get("review_mode", assessment["mode"])
        epoch = {"mode": review_mode, "full_merge": assessment["merged_count"] if review_mode == "full" else None,
                 "request": assessment["request_id"], "roots": assessment["roots"], "version": code_index.VERSION,
                 "guidance_hash": assessment.get("guidance_hash")}
        return tuple({"digest": digest({"parts": parts, "epoch": epoch}), "parts": parts} for parts in batches)

    def pending(self, repo, assessment):
        for unit in self.units(assessment):
            row = self.connection.execute("SELECT session_id, result_json, error FROM tiered_maintenance_units "
                                          "WHERE repo = ? AND digest = ?", (repo, unit["digest"])).fetchone()
            if not row or row[1] is None:
                return {**unit, "session_id": row[0] if row else None, "error": row[2] if row else None}
        return None

    def session(self, repo, unit, session):
        with self.connection:
            self.connection.execute("INSERT INTO tiered_maintenance_units (repo, digest, session_id) VALUES (?, ?, ?) "
                "ON CONFLICT(repo, digest) DO UPDATE SET session_id = excluded.session_id", (repo, unit["digest"], session))

    def fail(self, repo, unit, error):
        with self.connection:
            self.connection.execute("INSERT INTO tiered_maintenance_units (repo, digest, error) VALUES (?, ?, ?) "
                "ON CONFLICT(repo, digest) DO UPDATE SET error = excluded.error", (repo, unit["digest"], error))

    def complete(self, repo, unit, output, loader):
        lines = [line[len(RESULT_PREFIX):] for line in output.splitlines() if line.startswith(RESULT_PREFIX)]
        if len(lines) != 1:
            raise TieredReviewError("정밀 묶음은 MODULE_REVIEW_RESULT JSON 한 줄을 반환해야 합니다.")
        try:
            value = json.loads(lines[0], object_pairs_hook=_unique)
        except ValueError as error:
            raise TieredReviewError("정밀 묶음 JSON이 잘못됐습니다.") from error
        if not isinstance(value, dict) or set(value) != {"digest", "reviews", "findings"} or value["digest"] != unit["digest"]:
            raise TieredReviewError("정밀 묶음 identity 또는 필드가 다릅니다.")
        parts = unit["parts"]
        reviews, findings = value["reviews"], value["findings"]
        if not isinstance(reviews, list) or len(reviews) != len(parts) or not isinstance(findings, list) or len(findings) > 64:
            raise TieredReviewError("정밀 모듈 검토가 누락됐거나 발견 형식이 다릅니다.")
        allowed = {path: record for part in parts for path, record in part["records"].items()}
        def citation(item, paths):
            if (not isinstance(item, dict) or set(item) != {"path", "excerpt"}
                    or not isinstance(item["path"], str) or item["path"] not in paths):
                raise TieredReviewError("정밀 근거 경로가 검토 범위 밖입니다.")
            text = loader(item["path"])
            if hashlib.sha256(text).hexdigest() != allowed[item["path"]]["content_hash"]:
                raise TieredReviewError("정밀 근거의 원본 hash가 다릅니다.")
            excerpt = item["excerpt"]
            decoded = text.decode("utf-8", "replace")
            if not isinstance(excerpt, str) or not min(12, len(decoded)) <= len(excerpt) <= 1200 or excerpt not in decoded:
                raise TieredReviewError("정밀 인용이 실제 원본에서 확인되지 않습니다.")
        for review, part in zip(reviews, parts):
            if not isinstance(review, dict) or set(review) != {"module", "summary", "citations"} or review["module"] != part["module"]:
                raise TieredReviewError("정밀 검토의 모듈 순서·범위가 다릅니다.")
            _note(review["summary"])
            if not isinstance(review["citations"], list) or not 1 <= len(review["citations"]) <= 16:
                raise TieredReviewError("각 정밀 모듈의 원본 인용이 필요합니다.")
            for item in review["citations"]:
                citation(item, part["records"])
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {"path", "excerpt", "reason"}:
                raise TieredReviewError("정밀 발견의 필드가 다릅니다.")
            _note(finding["reason"])
            citation({"path": finding["path"], "excerpt": finding["excerpt"]}, allowed)
        with self.connection:
            self.connection.execute("INSERT INTO tiered_maintenance_units (repo, digest, result_json) VALUES (?, ?, ?) "
                "ON CONFLICT(repo, digest) DO UPDATE SET result_json = excluded.result_json, error = NULL, session_id = NULL",
                (repo, unit["digest"], code_index.encode(value)))

    def evidence(self, repo, cycle, revision):
        assessment = self.active(repo, cycle, revision)
        if assessment is None:
            return None
        receipts = []
        for unit in self.units(assessment):
            row = self.connection.execute("SELECT result_json FROM tiered_maintenance_units WHERE repo = ? AND digest = ?",
                                          (repo, unit["digest"])).fetchone()
            if row is None or row[0] is None:
                raise TieredReviewError("정밀 모듈의 검토가 아직 완료되지 않았습니다.")
            receipts.append(json.loads(row[0]))
        return {**assessment, "receipts": receipts}

    def promote(self, repo, evidence):
        # Caller owns the existing source/publication validation and transaction.
        current = self.active(repo, evidence["cycle"], evidence["source_revision"])
        if current is None or current["digest"] != evidence["digest"] or self.evidence(
                repo, evidence["cycle"], evidence["source_revision"]) != evidence:
            raise TieredReviewError("보존된 정밀 근거가 현재 평가와 다릅니다.")
        prior = self.success(repo)
        full_count = evidence["merged_count"] if evidence["mode"] == "full" else prior["full_merged_count"] if prior else None
        full_sha = evidence["source_revision"] if evidence["mode"] == "full" else prior["full_source_revision"] if prior else None
        self.connection.execute("INSERT OR REPLACE INTO tiered_maintenance_success VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo, code_index.encode(evidence["records"]), code_index.encode(evidence["roots"]),
             evidence["source_revision"], full_count, full_sha, evidence["digest"]))
        if evidence["request_id"]:
            self.connection.execute("DELETE FROM tiered_maintenance_requests WHERE repo = ? AND request_id = ?",
                                    (repo, evidence["request_id"]))


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def has_assessment(connection, repo, cycle, revision):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tiered_maintenance_assessments'").fetchone()
    return bool(exists and connection.execute("SELECT 1 FROM tiered_maintenance_assessments WHERE repo = ? AND cycle = ? "
        "AND source_revision = ?", (repo, cycle, revision)).fetchone())


def _note(value):
    if not isinstance(value, str) or not 12 <= len(value.strip()) <= 2000:
        raise TieredReviewError("모듈 판단에는 12~2000자의 구체적인 근거가 필요합니다.")
