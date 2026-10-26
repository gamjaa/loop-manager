"""Deterministic maintenance selection and durable module-level model receipts."""
from __future__ import annotations

from loop_manager_internal.application.result_normalization import bind_identity, tagged_json

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.maintenance.maintenance_file_inventory import cached_source_inventory, root_placement_inventory

RESULT_PREFIX = "MODULE_REVIEW_RESULT: "
RESULT_PROTOCOL = 2
ROOT_RESULT_PROTOCOL = 1
MAX_UNIT_BYTES = 160_000
MAX_MODULES = 8
MAX_OVERVIEW_SYMBOLS = 256
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiered_maintenance_success (
    repo TEXT PRIMARY KEY, records_json TEXT NOT NULL, roots_json TEXT NOT NULL,
    source_revision TEXT NOT NULL, full_merged_count INTEGER, full_source_revision TEXT,
    assessment_digest TEXT NOT NULL, root_reviews_json TEXT, guidance_hash TEXT);
CREATE TABLE IF NOT EXISTS tiered_maintenance_assessments (
    repo TEXT PRIMARY KEY, cycle INTEGER NOT NULL, source_revision TEXT NOT NULL,
    digest TEXT NOT NULL, assessment_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tiered_maintenance_units (
    repo TEXT NOT NULL, digest TEXT NOT NULL, session_id TEXT, result_json TEXT,
    error TEXT, PRIMARY KEY (repo, digest));
CREATE TABLE IF NOT EXISTS tiered_maintenance_result_audit (
    repo TEXT NOT NULL, digest TEXT NOT NULL, protocol INTEGER NOT NULL,
    source_revision TEXT, scope_json TEXT NOT NULL, raw_result TEXT NOT NULL,
    normalized_result TEXT NOT NULL, PRIMARY KEY (repo, digest));
CREATE TABLE IF NOT EXISTS tiered_maintenance_requests (
    repo TEXT PRIMARY KEY, request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tiered_maintenance_root_plan_links (
    repo TEXT NOT NULL, cycle INTEGER NOT NULL, source_revision TEXT NOT NULL,
    assessment_digest TEXT NOT NULL, links_json TEXT NOT NULL,
    PRIMARY KEY (repo, cycle, source_revision));
"""


class TieredReviewError(ValueError):
    pass


def ensure_schema(connection):
    # executescript would commit an outer publication transaction.
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tiered_maintenance_success)")}
    if "root_reviews_json" not in columns:
        connection.execute("ALTER TABLE tiered_maintenance_success ADD COLUMN root_reviews_json TEXT")
    if "guidance_hash" not in columns:
        connection.execute("ALTER TABLE tiered_maintenance_success ADD COLUMN guidance_hash TEXT")


def digest(value) -> str:
    return hashlib.sha256(code_index.encode(value).encode()).hexdigest()


def _review_scope(unit):
    return {key: value for key, value in unit.items() if key not in {"session_id", "error"}}


def plan_content_hash(contents: bytes) -> str:
    """Keep ExecPlan identity stable across the publication path's LF/CRLF conversion."""
    return hashlib.sha256(contents.replace(b"\r\n", b"\n")).hexdigest()


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
        "whitespace_only": facts.get("whitespace_only", False),
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
    base: str | None = None
    root_source: dict | None = None
    root_placements: tuple[dict, ...] = ()
    root_reviews: tuple[dict, ...] | None = None
    root_classifications: tuple[dict, ...] = ()
    root_guidance_hash: str | None = None


class TieredReviewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def success(self, repo):
        row = self.connection.execute("SELECT records_json, roots_json, source_revision, full_merged_count, "
            "full_source_revision, assessment_digest, root_reviews_json, guidance_hash "
            "FROM tiered_maintenance_success WHERE repo = ?", (repo,)).fetchone()
        return None if row is None else {"records": json.loads(row[0]), "roots": json.loads(row[1]),
            "source_revision": row[2], "full_merged_count": row[3], "full_source_revision": row[4],
            "digest": row[5], "root_reviews": json.loads(row[6]) if row[6] is not None else None,
            "guidance_hash": row[7]}

    def select(self, repo, repository, base, revision, interval, merged_count, *, force_full=False):
        if interval < 0:
            raise TieredReviewError("정밀 점검 주기는 0 이상의 정수여야 합니다.")
        inventory = cached_source_inventory(self.connection, repository, base, revision, include_uncertain=True)
        index = code_index.build_index(self.connection, repo + ":remote", inventory,
                                      lambda source: code_index.git(repository, "show", f"{revision}:{source.path}"))
        placements = tuple({"path": root.path, "category": root.category, "reason": root.reason}
                           for root in root_placement_inventory(inventory))
        classifications = tuple(asdict(item) for item in getattr(inventory, "classifications", ()))
        roots = tuple({"path": item["path"], "category": item["category"]} for item in placements)
        previous = self.success(repo)
        root_source, root_reviews, root_guidance_hash = None, (), None
        if placements:
            root_guidance_hash = common_guidance.effective_planning_hash(self.connection, repo)
            from loop_manager_internal.storage.maintenance_file_ledger import (
                FileReviewRepository, root_review_source, validate_root_reviews,
            )
            root_source = asdict(root_review_source(repository, inventory))
            root_reviews = FileReviewRepository(self.connection).reusable_root_reviews(repo, inventory)
            if root_reviews is None and previous is not None and previous["root_reviews"] is not None:
                if (previous["source_revision"] == revision
                        and previous["guidance_hash"] == root_guidance_hash):
                    try:
                        root_reviews = validate_root_reviews(repository, inventory, previous["root_reviews"])
                    except (ValueError, TypeError):
                        root_reviews = None
        request = self.connection.execute("SELECT request_id FROM tiered_maintenance_requests WHERE repo = ?", (repo,)).fetchone()
        request_id = str(request[0]) if request else None

        def selected(required, mode, reasons, paths, selected_request=None):
            return Selection(required, mode, reasons, paths, index, roots, merged_count, selected_request,
                             base, root_source, placements, root_reviews, classifications, root_guidance_hash)

        root_review_required = bool(placements and root_reviews is None)

        if interval == 0 and not force_full and request_id is None:
            return selected(False, "targeted" if root_review_required else "fast", ("disabled",), ())
        count = previous["full_merged_count"] if previous else None
        if count is not None and merged_count < count:
            raise TieredReviewError("전체 정밀 Merge 기준이 현재 이력보다 큽니다.")
        if force_full or request_id or count is None or (interval > 0 and merged_count - count >= interval):
            reason = "requested_full" if force_full or request_id else "initial_full" if count is None else "periodic_full"
            return selected(True, "full", (reason,), tuple(index.records), request_id)
        old = previous["records"]
        changed = {path for path in old.keys() | index.records.keys() if old.get(path) != index.records.get(path)}
        if not changed and list(roots) == previous["roots"]:
            if root_review_required:
                return selected(True, "targeted", ("root_review_required",), ())
            return selected(False, "fast", ("same_input",), ())
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
            if root_review_required:
                # The module change remains eligible for the static fast path;
                # only the missing root investigation needs a model unit.
                return selected(True, "targeted", ("root_review_required",), ())
            return selected(True, "fast", ("known_incremental_change",), tuple(sorted(changed & index.records.keys())))
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
        return Selection(True, "targeted", tuple(sorted(reasons)), selected, index, roots, merged_count,
                         None, base, root_source, placements, root_reviews, classifications, root_guidance_hash)

    def prepare(self, repo, cycle, selection: Selection, *, cycle_id=None, review_mode=None,
                guidance_hash=None):
        current_guidance = common_guidance.effective_planning_hash(self.connection, repo)
        if (selection.root_reviews is not None and selection.root_guidance_hash is not None
                and selection.root_guidance_hash != current_guidance):
            selection = replace(selection, root_reviews=None)
        if selection.mode == "fast" and selection.root_placements and selection.root_reviews is None:
            selection = replace(selection, mode="targeted", reasons=("root_review_required",), paths=())
        placements = list(selection.root_placements)
        root_reviews = selection.root_reviews
        if not placements:
            root_reviews = ()
        value = {"version": 1, "analysis_version": code_index.VERSION, "mode": selection.mode, "reasons": list(selection.reasons),
            "records": selection.index.records, "roots": list(selection.roots), "paths": list(selection.paths),
            "merged_count": selection.merged_count, "request_id": selection.request_id,
            "source_revision": selection.index.revision, "cycle": cycle, "cycle_id": cycle_id,
            "review_mode": review_mode or selection.mode, "base": selection.base,
            "root_source": selection.root_source, "root_placements": placements,
            "root_reviews": list(root_reviews) if root_reviews is not None else None,
            "root_classifications": list(selection.root_classifications),
            "root_guidance_hash": current_guidance}
        value["guidance_hash"] = guidance_hash or current_guidance
        value["digest"] = digest(value)
        row = self.connection.execute("SELECT cycle, source_revision, digest, assessment_json FROM tiered_maintenance_assessments "
                                      "WHERE repo = ?", (repo,)).fetchone()
        if row and (row[0], row[1]) == (cycle, selection.index.revision):
            saved = json.loads(row[3])
            if saved["roots"] != value["roots"]:
                raise TieredReviewError("같은 assessment identity의 코드맵이 달라졌습니다.")
            for key in ("base", "root_source", "root_placements", "root_classifications"):
                if key in saved and saved[key] != value[key]:
                    raise TieredReviewError("같은 assessment identity의 루트 조사 범위가 달라졌습니다.")
            saved_root_guidance = saved.get("root_guidance_hash", saved.get("guidance_hash"))
            root_guidance_changed = bool(saved.get("root_placements")
                                          and saved_root_guidance != value["root_guidance_hash"])
            if "root_reviews" in saved:
                if (saved["root_reviews"] is not None and value["root_reviews"] is not None
                        and not root_guidance_changed and saved["root_reviews"] != value["root_reviews"]):
                    raise TieredReviewError("같은 assessment identity의 루트 조사 결과가 달라졌습니다.")
                if saved["root_reviews"] is not None and not root_guidance_changed:
                    value["root_reviews"] = saved["root_reviews"]
            upgraded_roots = any(key not in saved for key in
                                 ("base", "root_source", "root_placements", "root_reviews", "root_classifications",
                                  "root_guidance_hash"))
            mode_corrected = (saved.get("mode") == "fast" and value["mode"] == "targeted"
                              and bool(value["root_placements"]) and value["root_reviews"] is None)
            root_reviews_added = (bool(saved.get("root_placements")) and saved.get("root_reviews") is None
                                  and value["root_reviews"] is not None)
            if upgraded_roots or root_guidance_changed or mode_corrected or root_reviews_added:
                saved = {**saved, "base": value["base"], "root_source": value["root_source"],
                         "root_placements": value["root_placements"],
                         "root_reviews": value["root_reviews"],
                         "root_classifications": value["root_classifications"],
                         "root_guidance_hash": value["root_guidance_hash"]}
                if mode_corrected:
                    saved.update({key: value[key] for key in
                                  ("mode", "reasons", "paths", "review_mode", "root_reviews")})
                saved.pop("digest", None)
                saved["digest"] = digest(saved)
            if saved["records"] != value["records"]:
                identities = lambda records: {path: (item["content_hash"], item["mode"])
                                               for path, item in records.items()}
                if (saved.get("analysis_version", 1) == code_index.VERSION
                        or identities(saved["records"]) != identities(value["records"])):
                    raise TieredReviewError("같은 assessment identity의 코드맵이 달라졌습니다.")
                # Reparse the same immutable sources after an analysis upgrade,
                # retaining the original task scope and provenance.
                previous = saved
                saved = {**saved, "records": value["records"], "analysis_version": code_index.VERSION,
                         "prior_analysis_assessment": previous}
                saved.pop("digest", None)
                saved["digest"] = digest(saved)
            if saved.get('guidance_hash'):
                if saved["digest"] != row[2] or code_index.encode(saved) != row[3]:
                    with self.connection:
                        self.connection.execute("UPDATE tiered_maintenance_assessments SET digest = ?, assessment_json = ? "
                            "WHERE repo = ?", (saved["digest"], code_index.encode(saved), repo))
                        if saved.get("root_placements") and saved.get("root_reviews") is None:
                            self._preserve_root_unit(repo, row[3], saved)
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
            if row and (row[0], row[1]) == (cycle, selection.index.revision):
                old = json.loads(row[3])
                if (old.get("root_placements") and value.get("root_placements")
                        and value.get("root_reviews") is None):
                    self._preserve_root_unit(repo, row[3], value)
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
            batches = []
        else:
            batches = None
        modules = {}
        if assessment["mode"] != "fast":
            for path in assessment["paths"]:
                record = _model_record(assessment["records"][path])
                if not _confirmed_blank(record):
                    modules.setdefault(code_index.module_group(path), {})[path] = record
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
                 "guidance_hash": assessment.get("guidance_hash"), "protocol": RESULT_PROTOCOL}
        units = [{"digest": digest({"parts": parts, "epoch": epoch}), "parts": parts} for parts in batches]
        if assessment["mode"] == "fast" and assessment.get("root_placements") and assessment.get("root_reviews") is None:
            raise TieredReviewError("fast 평가에는 미검토 루트 unit을 둘 수 없습니다.")
        root_unit = _root_review_unit(assessment)
        if root_unit is not None:
            units.append(root_unit)
        return tuple(units)

    def _preserve_root_unit(self, repo, prior_json, assessment):
        """Carry a root result or session across a mode-only assessment correction."""
        prior = json.loads(prior_json)
        if (prior.get("root_reviews") is not None or assessment.get("root_reviews") is not None
                or not prior.get("root_placements") or not assessment.get("root_placements")):
            return
        prior_guidance = prior.get("root_guidance_hash", prior.get("guidance_hash"))
        same_guidance = prior_guidance == assessment.get("root_guidance_hash")
        old_unit = _root_review_unit(prior)
        new_unit = _root_review_unit(assessment)
        if old_unit is None or new_unit is None or old_unit["digest"] == new_unit["digest"]:
            return
        row = self.connection.execute("SELECT session_id, result_json, error FROM tiered_maintenance_units "
                                      "WHERE repo = ? AND digest = ?", (repo, old_unit["digest"])).fetchone()
        if row is None:
            return
        if row[1] is not None:
            if not same_guidance:
                return
            audit = self.connection.execute("SELECT protocol, scope_json, raw_result, normalized_result, source_revision "
                "FROM tiered_maintenance_result_audit WHERE repo = ? AND digest = ?",
                (repo, old_unit["digest"])).fetchone()
            try:
                if (audit is None or audit[0] != ROOT_RESULT_PROTOCOL or _review_scope(json.loads(audit[1])) != old_unit
                        or audit[4] != assessment["source_revision"]
                        or json.loads(audit[3]) != json.loads(row[1])):
                    return
            except (AttributeError, TypeError, ValueError):
                return
            try:
                self.complete(repo, new_unit, audit[2], lambda _path: b"")
            except TieredReviewError:
                return
            return
        self.connection.execute("INSERT OR IGNORE INTO tiered_maintenance_units "
            "(repo, digest, session_id, error) VALUES (?, ?, ?, ?)",
            (repo, new_unit["digest"], row[0], row[2]))

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
        active = self.latest(repo)
        expected = (next((candidate for candidate in self.units(active)
                          if candidate["digest"] == unit.get("digest")), None)
                    if active is not None else None)
        supplied = _review_scope(unit)
        if active is None or expected != supplied:
            raise TieredReviewError("정밀 결과의 scope가 현재 assessment와 다릅니다.")
        unit = supplied
        is_root = unit.get("kind") == "root_placement"
        if is_root:
            value = parse_root_review_result(repo, active, unit, output, current=True)
            scope = code_index.encode(unit)
            protocol = ROOT_RESULT_PROTOCOL
        else:
            value = parse_module_result(unit, output, loader)
            scope = code_index.encode(unit["parts"])
            protocol = RESULT_PROTOCOL
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO tiered_maintenance_result_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, unit["digest"], protocol, active["source_revision"], scope, output, code_index.encode(value)))
            self.connection.execute("INSERT INTO tiered_maintenance_units (repo, digest, result_json) VALUES (?, ?, ?) "
                "ON CONFLICT(repo, digest) DO UPDATE SET result_json = excluded.result_json, error = NULL, session_id = NULL",
                (repo, unit["digest"], code_index.encode(value)))

    def evidence(self, repo, cycle, revision):
        assessment = self.active(repo, cycle, revision)
        if assessment is None:
            return None
        receipts = []
        root_reviews = assessment.get("root_reviews")
        inventory = None
        if assessment.get("root_placements"):
            try:
                inventory = _assessment_inventory(repo, assessment)
                from loop_manager_internal.storage.maintenance_file_ledger import root_review_source
                expected_placements = [asdict(item) for item in root_placement_inventory(inventory)]
                if (expected_placements != assessment["root_placements"]
                        or asdict(root_review_source(Path(repo), inventory)) != assessment["root_source"]):
                    raise TieredReviewError("루트 조사 inventory·source identity가 평가 입력과 다릅니다.")
            except (ValueError, OSError, sqlite3.Error) as error:
                if isinstance(error, TieredReviewError):
                    raise
                raise TieredReviewError("루트 조사 원본을 다시 확인할 수 없습니다.") from error
        for unit in self.units(assessment):
            row = self.connection.execute("SELECT result_json FROM tiered_maintenance_units WHERE repo = ? AND digest = ?",
                                          (repo, unit["digest"])).fetchone()
            if row is None or row[0] is None:
                raise TieredReviewError("정밀 모듈 또는 루트 조사의 검토가 아직 완료되지 않았습니다.")
            audit = self.connection.execute(
                "SELECT protocol, scope_json, raw_result, normalized_result, source_revision FROM tiered_maintenance_result_audit "
                "WHERE repo = ? AND digest = ?", (repo, unit["digest"])).fetchone()
            try:
                receipt = json.loads(row[0])
                is_root = unit.get("kind") == "root_placement"
                expected_protocol = ROOT_RESULT_PROTOCOL if is_root else RESULT_PROTOCOL
                expected_scope = unit if is_root else unit["parts"]
                stored_scope = json.loads(audit[1]) if audit is not None else None
                if is_root and isinstance(stored_scope, dict):
                    stored_scope = _review_scope(stored_scope)
                if (audit is None or audit[0] != expected_protocol or stored_scope != expected_scope
                        or not isinstance(audit[4], str) or not audit[4].strip()
                        or is_root and audit[4] != assessment["source_revision"]):
                    raise ValueError("result audit scope differs")
                normalized = (parse_root_review_result(repo, assessment, unit, audit[2], inventory=inventory)
                              if is_root else parse_module_result(unit, audit[2]))
                if normalized != receipt or json.loads(audit[3]) != receipt:
                    raise ValueError("raw and normalized results differ")
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                raise TieredReviewError("보존된 모듈 결과의 원문·정규화·scope가 다릅니다.") from error
            if is_root:
                if root_reviews is not None and root_reviews != receipt["root_reviews"]:
                    raise TieredReviewError("assessment와 보존된 루트 조사 결과가 다릅니다.")
                root_reviews = receipt["root_reviews"]
            else:
                receipts.append(receipt)
        if root_reviews is None:
            if assessment.get("root_placements"):
                raise TieredReviewError("모든 루트 항목의 실제 조사 근거가 필요합니다.")
            root_reviews = []
        if inventory is not None:
            from loop_manager_internal.storage.maintenance_file_ledger import validate_root_reviews
            try:
                root_reviews = list(validate_root_reviews(Path(repo), inventory, root_reviews))
            except (ValueError, TypeError) as error:
                raise TieredReviewError("보존된 루트 조사 근거가 현재 원본과 다릅니다.") from error
        link_row = self.connection.execute(
            "SELECT assessment_digest, links_json FROM tiered_maintenance_root_plan_links "
            "WHERE repo = ? AND cycle = ? AND source_revision = ?", (repo, cycle, revision),
        ).fetchone()
        if link_row and link_row[0] != assessment["digest"]:
            raise TieredReviewError("루트 계획 연결이 다른 assessment identity에 결합됐습니다.")
        links = json.loads(link_row[1]) if link_row else None
        if links is not None:
            _validate_root_plan_links(root_reviews, links, required=bool(_root_move_candidates(root_reviews)))
        return {**assessment, "receipts": receipts, "root_reviews": root_reviews,
                "root_plan_links": links}

    def record_root_plan_links(self, repo, cycle, revision, links, *, plan_repository=None):
        evidence = self.evidence(repo, cycle, revision)
        if evidence is None:
            raise TieredReviewError("루트 후보를 연결할 정밀 평가가 없습니다.")
        candidates = _root_move_candidates(evidence["root_reviews"])
        normalized = _validate_root_plan_links(evidence["root_reviews"], links,
                                               required=bool(candidates))
        if candidates:
            root = Path(plan_repository or repo).resolve()
            for item in normalized:
                relative = item["plan_path"]
                target = root / relative
                if (target.is_symlink() or not target.resolve().is_relative_to(root)
                        or any(parent.is_symlink() for parent in target.parents if parent != root)
                        or not target.is_file()):
                    raise TieredReviewError("루트 이동 계획 경로가 안전한 일반 파일이 아닙니다.")
                if plan_content_hash(target.read_bytes()) != item["plan_hash"]:
                    raise TieredReviewError("루트 이동 계획이 평가 뒤 변경됐습니다.")
            encoded = code_index.encode(normalized)
            current = self.connection.execute(
                "SELECT assessment_digest, links_json FROM tiered_maintenance_root_plan_links "
                "WHERE repo = ? AND cycle = ? AND source_revision = ?", (repo, cycle, revision),
            ).fetchone()
            if current:
                try:
                    saved_links = code_index.encode(json.loads(current[1]))
                except (TypeError, ValueError) as error:
                    raise TieredReviewError("저장된 루트 계획 연결이 손상됐습니다.") from error
                if current[0] != evidence["digest"] or saved_links != encoded:
                    raise TieredReviewError("같은 평가의 루트 계획 연결이 달라졌습니다.")
                return normalized
            with self.connection:
                self.connection.execute("INSERT INTO tiered_maintenance_root_plan_links "
                    "VALUES (?, ?, ?, ?, ?)", (repo, cycle, revision, evidence["digest"], encoded))
        elif normalized:
            raise TieredReviewError("이동 후보가 없는 평가에 루트 계획 연결을 기록할 수 없습니다.")
        return normalized

    def promote(self, repo, evidence):
        # Caller owns the existing source/publication validation and transaction.
        current = self.active(repo, evidence["cycle"], evidence["source_revision"])
        if current is None or current["digest"] != evidence["digest"] or self.evidence(
                repo, evidence["cycle"], evidence["source_revision"]) != evidence:
            raise TieredReviewError("보존된 정밀 근거가 현재 평가와 다릅니다.")
        _validate_root_plan_links(evidence["root_reviews"], evidence.get("root_plan_links"),
                                  required=bool(_root_move_candidates(evidence["root_reviews"])))
        prior = self.success(repo)
        full_count = evidence["merged_count"] if evidence["mode"] == "full" else prior["full_merged_count"] if prior else None
        full_sha = evidence["source_revision"] if evidence["mode"] == "full" else prior["full_source_revision"] if prior else None
        self.connection.execute("INSERT OR REPLACE INTO tiered_maintenance_success "
            "(repo, records_json, roots_json, source_revision, full_merged_count, full_source_revision, "
            "assessment_digest, root_reviews_json, guidance_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, code_index.encode(evidence["records"]), code_index.encode(evidence["roots"]),
             evidence["source_revision"], full_count, full_sha, evidence["digest"],
             code_index.encode(evidence["root_reviews"]),
             evidence.get("root_guidance_hash", evidence.get("guidance_hash"))))
        if evidence["request_id"]:
            self.connection.execute("DELETE FROM tiered_maintenance_requests WHERE repo = ? AND request_id = ?",
                                    (repo, evidence["request_id"]))


def parse_module_result(unit, output, loader=None):
    """Validate judgments on first acceptance and when replaying the same raw audit."""
    try:
        value = tagged_json(output, RESULT_PREFIX)
        if not isinstance(value, dict):
            raise ValueError("module result must be an object")
        value = bind_identity(value, {"digest": unit["digest"]})
    except (ValueError, RecursionError) as error:
        raise TieredReviewError("정밀 묶음 JSON 또는 identity가 잘못됐습니다.") from error
    if set(value) != {"digest", "reviews", "findings"}:
        raise TieredReviewError("정밀 묶음 identity 또는 필드가 다릅니다.")
    parts = unit["parts"]
    reviews, findings = value["reviews"], value["findings"]
    if not isinstance(reviews, list) or len(reviews) != len(parts) or not isinstance(findings, list) or len(findings) > 64:
        raise TieredReviewError("정밀 모듈 검토가 누락됐거나 발견 형식이 다릅니다.")
    allowed = {path: record for part in parts for path, record in part["records"].items()}
    def citation(item, paths, *, allow_blank=False):
        if (not isinstance(item, dict) or set(item) != {"path", "excerpt"}
                or not isinstance(item["path"], str) or item["path"] not in paths):
            raise TieredReviewError("정밀 근거 경로가 검토 범위 밖입니다.")
        if not isinstance(item["excerpt"], str) or (not item["excerpt"] and not allow_blank):
            raise TieredReviewError("정밀 원본 인용이 누락됐습니다.")
        if loader is None:
            return  # The audit preserves the previously source-validated quotes.
        text = loader(item["path"])
        if hashlib.sha256(text).hexdigest() != allowed[item["path"]]["content_hash"]:
            raise TieredReviewError("정밀 근거의 원본 hash가 다릅니다.")
        excerpt = item["excerpt"]
        decoded = text.decode("utf-8", "replace")
        if (not allow_blank and not decoded.strip()) or not isinstance(excerpt, str) or not min(12, len(decoded)) <= len(excerpt) <= 1200 or excerpt not in decoded:
            raise TieredReviewError("정밀 인용이 실제 원본에서 확인되지 않습니다.")
    for index, (review, part) in enumerate(zip(reviews, parts)):
        if isinstance(review, dict):
            try:
                review = bind_identity(review, {"module": part["module"]})
            except ValueError as error:
                raise TieredReviewError("정밀 검토의 모듈 identity가 다릅니다.") from error
            reviews[index] = review
        if not isinstance(review, dict) or set(review) != {"module", "summary", "citations"} or review["module"] != part["module"]:
            raise TieredReviewError("정밀 검토의 모듈 순서·범위가 다릅니다.")
        _note(review["summary"])
        if not isinstance(review["citations"], list) or not 1 <= len(review["citations"]) <= 16:
            raise TieredReviewError("각 정밀 모듈의 원본 인용이 필요합니다.")
        allow_blank = all(record["facts"].get("whitespace_only") is True for record in part["records"].values())
        for item in review["citations"]:
            citation(item, part["records"], allow_blank=allow_blank)
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"path", "excerpt", "reason"}:
            raise TieredReviewError("정밀 발견의 필드가 다릅니다.")
        _note(finding["reason"])
        citation({"path": finding["path"], "excerpt": finding["excerpt"]}, allowed)
    return value


def _assessment_inventory(repo, assessment):
    from loop_manager_internal.maintenance.maintenance_file_inventory import (
        ClassifiedPath, SourceInventory, TrackedSource,
    )
    revision = assessment["source_revision"]
    sources = tuple(TrackedSource(path, record["content_hash"], record["mode"], revision,
                                  "tiered module inventory")
                    for path, record in sorted(assessment["records"].items()))
    classifications = tuple(ClassifiedPath(**item) for item in assessment.get("root_classifications", ()))
    return SourceInventory(revision, sources, classifications)


def _root_review_unit(assessment):
    if assessment.get("root_reviews") is not None or not assessment.get("root_placements"):
        return None
    source = assessment.get("root_source")
    if not isinstance(source, dict) or assessment.get("base") is None:
        raise TieredReviewError("루트 조사 source identity가 없습니다.")
    root_epoch = {"assessment": assessment["digest"], "cycle": assessment["cycle"],
                  "source": source, "placements": assessment["root_placements"],
                  "guidance_hash": assessment.get("root_guidance_hash", assessment.get("guidance_hash")),
                  "protocol": ROOT_RESULT_PROTOCOL}
    return {"digest": digest({"kind": "root_placement", "epoch": root_epoch}),
            "kind": "root_placement", "source": source,
            "placements": assessment["root_placements"]}


def parse_root_review_result(repo, assessment, unit, output, *, inventory=None, current=False):
    """Reuse the file assessment's root result contract and exact Git citation validator."""
    from loop_manager_internal.maintenance.maintenance_file_review import parse_review, source_bytes
    from loop_manager_internal.maintenance.maintenance_file_inventory import (
        TrackedSource, root_placement_inventory, tracked_source_inventory,
    )
    from loop_manager_internal.storage.maintenance_file_ledger import (
        root_review_source, validate_root_reviews,
    )
    repository = Path(repo)
    try:
        if inventory is None:
            inventory = (tracked_source_inventory(repository, assessment["base"],
                         assessment["source_revision"], include_uncertain=True) if current
                         else _assessment_inventory(repo, assessment))
        placements = [asdict(item) for item in root_placement_inventory(inventory)]
        source = TrackedSource(**unit["source"])
        if (placements != unit["placements"] or asdict(root_review_source(repository, inventory)) != unit["source"]):
            raise TieredReviewError("루트 조사 source identity 또는 범위가 다릅니다.")
        if current:
            content = source_bytes(repository, assessment["base"], source)
        else:
            content = code_index.git(repository, "ls-tree", assessment["source_revision"])
        parsed = parse_review(output, source, content)
        reviewed = validate_root_reviews(repository, inventory, parsed["root_reviews"])
        return {"digest": unit["digest"], "root_reviews": list(reviewed)}
    except TieredReviewError:
        raise
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        raise TieredReviewError("루트 조사 결과를 현재 원본과 대조할 수 없습니다.") from error


def _root_move_candidates(reviews):
    from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates
    return root_move_candidates(reviews)


def _validate_root_plan_links(root_reviews, links, *, required):
    from loop_manager_internal.storage.maintenance_file_ledger import root_review_hash
    candidates = _root_move_candidates(root_reviews)
    if not candidates:
        if required:
            raise TieredReviewError("루트 이동 후보의 계획 연결이 필요하지만 후보를 확인할 수 없습니다.")
        if links not in (None, [], ()):
            raise TieredReviewError("이동 후보가 없는 루트 조사에 계획 연결이 있습니다.")
        return ()
    if not isinstance(links, (list, tuple)) or len(links) != len(candidates):
        raise TieredReviewError("모든 루트 이동 후보의 계획 연결이 필요합니다.")
    expected_fields = {"path", "destination", "root_review_hash", "status", "plan_id",
                       "plan_path", "plan_hash", "reason"}
    normalized = []
    seen = set()
    for item in links:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise TieredReviewError("루트 계획 연결의 필드가 다릅니다.")
        path = item["path"]
        root = candidates.get(path) if isinstance(path, str) else None
        if (root is None or path in seen or item["destination"] != root["destination"]
                or item["root_review_hash"] != root_review_hash(root)):
            raise TieredReviewError("루트 계획 연결이 현재 조사된 이동 후보와 다릅니다.")
        if not isinstance(item["status"], str) or item["status"] not in {"new_plan", "existing_plan"}:
            raise TieredReviewError("루트 이동 후보는 새 계획 또는 비종료 계획에 연결해야 합니다.")
        if (not isinstance(item["plan_id"], str) or not re.fullmatch(r"G-[0-9]+", item["plan_id"])
                or not isinstance(item["plan_path"], str) or not item["plan_path"].startswith("docs/plans/")
                or item["plan_path"].startswith("/") or "\\" in item["plan_path"]
                or any(part in {"", ".", ".."} for part in item["plan_path"].split("/"))
                or not re.fullmatch(r"[0-9a-f]{64}", str(item["plan_hash"]))):
            raise TieredReviewError("루트 이동 계획의 identity가 안전하지 않습니다.")
        _note(item["reason"])
        seen.add(path)
        normalized.append({key: item[key] for key in sorted(expected_fields)})
    if seen != set(candidates):
        raise TieredReviewError("루트 이동 후보의 계획 연결이 누락됐습니다.")
    return tuple(sorted(normalized, key=lambda item: item["path"]))


def _confirmed_blank(record):
    facts = record["facts"]
    return facts.get("whitespace_only") is True and not facts["uncertainties"]


def has_assessment(connection, repo, cycle, revision):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tiered_maintenance_assessments'").fetchone()
    return bool(exists and connection.execute("SELECT 1 FROM tiered_maintenance_assessments WHERE repo = ? AND cycle = ? "
        "AND source_revision = ?", (repo, cycle, revision)).fetchone())


def _note(value):
    if not isinstance(value, str) or not 12 <= len(value.strip()) <= 2000:
        raise TieredReviewError("모듈 판단에는 12~2000자의 구체적인 근거가 필요합니다.")
