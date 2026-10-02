"""Bounded, source-bound transport for provisional findings and plan comparisons.

Agent responses are small independent units. JSON records may span units; SQLite
owns the cursor and exact fragments. A sealed manifest replaces the large final
response, but never replaces semantic validation of the assembled records.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping

from loop_manager_internal.maintenance.maintenance_reconciliation import ReconciliationError, _note, _single_json

UNIT_PREFIX = "FINDINGS_UNIT: "
RESULT_PREFIX = "FINDINGS_RESULT: "
MAX_UNIT_BYTES = 24_000
MAX_RESULT_BYTES = 12_000


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class FindingsUnit:
    manifest_id: str
    index: int
    revision: str
    synthesis_digest: str
    session_id: str | None
    error: str | None
    receipt: dict | None


def unit_prompt(unit: FindingsUnit, manifest: Path) -> str:
    return (
        "유지보수 발견과 모든 비종료 계획의 범위 비교를 수집하라. 파일은 수정하지 마라. "
        "입력 manifest의 증거·계획·저장된 unit을 필요한 부분만 읽고 이어서 작업하라. "
        "작은 결과는 한 단위에 모두 담고 24000 bytes를 넘으면 여러 단위로 나눠라. "
        "발견 수나 비교 대상 계획을 잘라내지 마라. 발견 record에는 issue_key, targets, evidence "
        "(path,content_hash,mode,excerpt), relation_keys, boundary, context_reduction, integration_cost, "
        "status(existing_plan|new_plan), plan_id를 포함한다. "
        "root_placement 증거에서 category=internal_implementation이고 destination이 있는 모든 루트 후보는 "
        "독립 발견으로 기존 또는 새 계획에 연결하라. source 선정 여부나 문서·asset 제외 여부로 누락하지 마라. "
        "루트 발견 evidence는 {path,root_review_hash:루트 조사 JSON의 SHA256,excerpt:조사 결과의 정확한 12자 이상 인용}이며 "
        "해시는 ensure_ascii=False, sort_keys=True인 json.dumps의 UTF-8 byte로 계산한다. "
        "boundary에 조사한 destination과 import·상대 경로·launcher·설정 탐색·문서 링크·verify 보존 조건을 기록하라. "
        "필수·불확실 항목은 이동 후보가 아니다. plan_comparisons는 별도 comparison record로 "
        "모든 비종료 계획마다 plan_id, plan_excerpt(계획 원문 20자 이상 인용), "
        "finding_excerpt(boundary 20자 이상 인용), same_issue(boolean), reason을 작성하라. "
        "같은 문제의 기존 계획이 있으면 반드시 연결하고 새 계획을 중복 제안하지 마라. "
        "새 계획 ID는 계획 catalog와 충돌하지 않는 ID를 고르고 마지막 Planner가 실제 파일을 작성한다. "
        "각 record를 JSON 문자열 text로 보내라. 큰 record도 part=0부터 순서대로 나누고 마지막 part만 last=true다. "
        "이전 저장 record의 오류를 수정해야 하면 FINDINGS_RESTART: {manifest_id,index,reason}만 반환해 수집을 다시 시작하라. "
        "완료 record나 이미 저장된 part는 재전송하지 마라. 모든 발견·비교를 저장한 마지막 단위만 complete=true, "
        "그 전에는 counts와 summary가 null이다. 완료 counts는 전체 finding_count와 comparison_count다. "
        "문제가 없으면 parts=[]로 완료하고 근거 있는 summary를 남겨라.\n"
        f"input_manifest={manifest}\nmanifest_id={unit.manifest_id}\nindex={unit.index}\n"
        "FINDINGS_UNIT: {manifest_id,index,parts:[{kind:finding|comparison,issue_key,"
        "plan_id:comparison의 계획 ID 또는 finding일 때 null,part:0부터 연속 번호,"
        "text:JSON 원문 조각,last:boolean}],complete:boolean,"
        "counts:null|{finding_count,comparison_count},summary:null|20~2000자 요약} 한 줄 JSON만 반환하라."
        + ("\n이전 오류: " + unit.error[:2000] if unit.error else "")
    )


class FindingsRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        schema = """
            CREATE TABLE IF NOT EXISTS provisional_findings_streams (
                repo TEXT PRIMARY KEY, binding TEXT NOT NULL, generation INTEGER NOT NULL,
                manifest_id TEXT NOT NULL, next_unit INTEGER NOT NULL, session_id TEXT,
                validation_error TEXT, receipt_json TEXT);
            CREATE TABLE IF NOT EXISTS provisional_findings_units (
                repo TEXT NOT NULL, manifest_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(repo, manifest_id, unit_index));
            CREATE TABLE IF NOT EXISTS provisional_findings_parts (
                repo TEXT NOT NULL, manifest_id TEXT NOT NULL, kind TEXT NOT NULL,
                issue_key TEXT NOT NULL, plan_id TEXT NOT NULL, part INTEGER NOT NULL,
                text TEXT NOT NULL, last INTEGER NOT NULL,
                PRIMARY KEY(repo, manifest_id, kind, issue_key, plan_id, part));
        """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    def prepare(self, repo: str, cycle: int, revision: str, synthesis_digest: str,
                plans: Mapping[str, str], plan_texts: Mapping[str, str]) -> FindingsUnit:
        binding = digest([cycle, revision, synthesis_digest, dict(plans), dict(plan_texts)])
        with self.connection:
            row = self.connection.execute(
                "SELECT binding,generation,manifest_id,next_unit,session_id,validation_error,receipt_json "
                "FROM provisional_findings_streams WHERE repo=?", (repo,),
            ).fetchone()
            if row is None or row[0] != binding:
                generation = row[1] + 1 if row else 0
                identity = digest([binding, generation])
                self.connection.execute("INSERT OR REPLACE INTO provisional_findings_streams VALUES (?,?,?,?,0,NULL,NULL,NULL)",
                                        (repo, binding, generation, identity))
                row = (binding, generation, identity, 0, None, None, None)
        return FindingsUnit(row[2], row[3], revision, synthesis_digest, row[4], row[5],
                            json.loads(row[6]) if row[6] else None)

    def _current(self, repo: str, unit: FindingsUnit) -> None:
        row = self.connection.execute("SELECT manifest_id,next_unit,receipt_json FROM provisional_findings_streams WHERE repo=?", (repo,)).fetchone()
        if row is None or row[0] != unit.manifest_id or row[1] != unit.index or row[2] is not None:
            raise ReconciliationError("발견 수집 단위가 중복되거나 source·계획 집합이 바뀌었습니다.")

    def session(self, repo: str, unit: FindingsUnit, session_id: str) -> None:
        with self.connection:
            self._current(repo, unit)
            self.connection.execute("UPDATE provisional_findings_streams SET session_id=? WHERE repo=?", (session_id, repo))

    def fail(self, repo: str, unit: FindingsUnit, error: str) -> None:
        with self.connection:
            self._current(repo, unit)
            self.connection.execute("UPDATE provisional_findings_streams SET validation_error=? WHERE repo=?", (error[:2000], repo))

    def _part(self, repo: str, unit: FindingsUnit, value: dict, plans: Mapping[str, str]) -> None:
        if not isinstance(value, dict) or set(value) != {"kind", "issue_key", "plan_id", "part", "text", "last"}:
            raise ReconciliationError("발견 조각 필드가 다릅니다.")
        kind, issue, plan = value["kind"], _note(value["issue_key"], "문제 식별자", 4), value["plan_id"]
        if kind not in ("finding", "comparison") or (kind == "finding" and plan is not None) or (
            kind == "comparison" and (not isinstance(plan, str) or plan not in plans)):
            raise ReconciliationError("발견·비교 조각 대상이 다릅니다.")
        number, text = value["part"], value["text"]
        if type(number) is not int or number < 0 or type(value["last"]) is not bool or not isinstance(text, str) or not text:
            raise ReconciliationError("발견 조각 번호·내용이 유효하지 않습니다.")
        key = (repo, unit.manifest_id, kind, issue, plan or "")
        previous = self.connection.execute("SELECT part,text,last FROM provisional_findings_parts "
            "WHERE repo=? AND manifest_id=? AND kind=? AND issue_key=? AND plan_id=? ORDER BY part", key).fetchall()
        if number != len(previous) or any(row[2] for row in previous):
            raise ReconciliationError("발견 조각이 누락·중복됐거나 이미 완료됐습니다.")
        if value["last"]:
            try:
                record = json.loads("".join(row[1] for row in previous) + text)
            except ValueError as error:
                raise ReconciliationError("완료 발견 조각의 JSON이 유효하지 않습니다.") from error
            if not isinstance(record, dict) or (kind == "finding" and (record.get("issue_key") != issue or "plan_comparisons" in record)) or (
                kind == "comparison" and record.get("plan_id") != plan):
                raise ReconciliationError("완료 발견 record identity가 다릅니다.")
        self.connection.execute("INSERT INTO provisional_findings_parts VALUES (?,?,?,?,?,?,?,?)",
                                (*key, number, text, int(value["last"])))

    def materialize(self, repo: str, unit: FindingsUnit, plans: Mapping[str, str], summary: str) -> dict:
        records: dict[tuple, list] = {}
        for kind, issue, plan, number, text, last in self.connection.execute(
            "SELECT kind,issue_key,plan_id,part,text,last FROM provisional_findings_parts "
            "WHERE repo=? AND manifest_id=? ORDER BY kind,issue_key,plan_id,part", (repo, unit.manifest_id),
        ):
            records.setdefault((kind, issue, plan), []).append((number, text, last))
        decoded = {}
        for key, parts in records.items():
            if [part[0] for part in parts] != list(range(len(parts))) or not parts[-1][2] or any(part[2] for part in parts[:-1]):
                raise ReconciliationError("미완료 발견·비교 조각이 남았습니다.")
            decoded[key] = json.loads("".join(part[1] for part in parts))
        findings = {issue: value for (kind, issue, _plan), value in decoded.items() if kind == "finding"}
        comparisons = {(issue, plan): value for (kind, issue, plan), value in decoded.items() if kind == "comparison"}
        if set(comparisons) != {(issue, plan) for issue in findings for plan in plans}:
            raise ReconciliationError("모든 발견과 고정 비종료 계획의 비교가 필요합니다.")
        return {"source_revision": unit.revision,
                "findings": [{**value, "plan_comparisons": [comparisons[issue, plan] for plan in sorted(plans)]}
                             for issue, value in sorted(findings.items())],
                "no_issue_reason": None if findings else {"synthesis_digest": unit.synthesis_digest, "summary": summary}}

    def complete(self, repo: str, unit: FindingsUnit, output: str, plans: Mapping[str, str],
                 validate: Callable[[dict], object]) -> None:
        if len(output.encode()) > MAX_UNIT_BYTES:
            raise ReconciliationError("발견 수집 응답이 byte 제한을 넘었습니다.")
        if any(line.startswith("FINDINGS_RESTART: ") for line in output.splitlines()):
            restart = _single_json(output, "FINDINGS_RESTART: ")
            if set(restart) != {"manifest_id", "index", "reason"} or restart["manifest_id"] != unit.manifest_id or restart["index"] != unit.index:
                raise ReconciliationError("발견 수집 재시작 identity가 다릅니다.")
            reason = _note(restart["reason"], "수집 재시작 근거", 20)
            with self.connection:
                self._current(repo, unit)
                binding, generation = self.connection.execute("SELECT binding,generation FROM provisional_findings_streams WHERE repo=?", (repo,)).fetchone()
                self.connection.execute("UPDATE provisional_findings_streams SET generation=?,manifest_id=?,next_unit=0,session_id=NULL,validation_error=?,receipt_json=NULL WHERE repo=?",
                    (generation + 1, digest([binding, generation + 1]), reason, repo))
            return
        value = _single_json(output, UNIT_PREFIX)
        if (set(value) != {"manifest_id", "index", "parts", "complete", "counts", "summary"}
            or value["manifest_id"] != unit.manifest_id or type(value["index"]) is not int or value["index"] != unit.index
            or not isinstance(value["parts"], list) or type(value["complete"]) is not bool):
            raise ReconciliationError("발견 수집 identity·필드가 다릅니다.")
        if not value["complete"] and (not value["parts"] or value["counts"] is not None or value["summary"] is not None):
            raise ReconciliationError("미완료 발견 수집의 개수·요약이 다릅니다.")
        with self.connection:
            self._current(repo, unit)
            for part in value["parts"]:
                self._part(repo, unit, part, plans)
            receipt = None
            if value["complete"]:
                summary = _note(value["summary"], "발견 요약", 20)
                assembled = self.materialize(repo, unit, plans, summary)
                counts = {"finding_count": len(assembled["findings"]),
                          "comparison_count": len(assembled["findings"]) * len(plans)}
                if value["counts"] != counts or any(type(v) is not int for v in value["counts"].values()):
                    raise ReconciliationError("발견·비교 전체 개수가 다릅니다.")
                validate(assembled)
                receipt = {"manifest_id": unit.manifest_id, "digest": digest(assembled), **counts,
                           "unit_count": unit.index + 1, "summary": summary}
            self.connection.execute("INSERT INTO provisional_findings_units VALUES (?,?,?,?)",
                                    (repo, unit.manifest_id, unit.index, encode(value)))
            self.connection.execute("UPDATE provisional_findings_streams SET next_unit=next_unit+1,session_id=NULL,"
                "validation_error=NULL,receipt_json=? WHERE repo=?", (encode(receipt) if receipt else None, repo))

    def resolve(self, repo: str, unit: FindingsUnit, output: str, plans: Mapping[str, str]) -> dict:
        if len(output.encode()) > MAX_RESULT_BYTES:
            raise ReconciliationError("최종 발견 manifest 응답이 byte 제한을 넘었습니다.")
        value = _single_json(output, RESULT_PREFIX)
        current = self.connection.execute(
            "SELECT manifest_id,next_unit,receipt_json FROM provisional_findings_streams WHERE repo=?",
            (repo,)).fetchone()
        if (unit.receipt is None or encode(value) != encode(unit.receipt) or current is None
            or current[0] != unit.manifest_id or current[1] != unit.index
            or current[2] != encode(unit.receipt)):
            raise ReconciliationError("최종 발견 manifest가 미완료·누락·오래된 결과입니다.")
        payload = self.materialize(repo, unit, plans, unit.receipt["summary"])
        rows = self.connection.execute("SELECT unit_index FROM provisional_findings_units WHERE repo=? AND manifest_id=? ORDER BY unit_index",
                                       (repo, unit.manifest_id)).fetchall()
        if [row[0] for row in rows] != list(range(unit.index)) or unit.receipt["unit_count"] != unit.index or digest(payload) != unit.receipt["digest"]:
            raise ReconciliationError("저장된 발견 단위 또는 digest가 다릅니다.")
        return payload

    def export(self, repo: str, unit: FindingsUnit, plans: Mapping[str, str], plan_texts: Mapping[str, str],
               evidence_manifest: Path) -> Path:
        directory = evidence_manifest.parent / "findings" / unit.manifest_id
        directory.mkdir(parents=True, exist_ok=True)
        catalog = directory / "plans.jsonl"
        if not catalog.exists():
            with catalog.open("w", encoding="utf-8") as stream:
                for index, (plan, status) in enumerate(sorted(plans.items())):
                    name = f"plan-{index:08d}.txt"
                    (directory / name).write_text(plan_texts[plan], encoding="utf-8")
                    stream.write(encode({"plan_id": plan, "status": status, "path": name}) + "\n")
        for index, raw in self.connection.execute("SELECT unit_index,payload_json FROM provisional_findings_units WHERE repo=? AND manifest_id=?",
                                                  (repo, unit.manifest_id)):
            path = directory / f"unit-{index:08d}.json"
            if not path.exists():
                path.write_text(raw, encoding="utf-8")
        manifest = directory / "manifest.json"
        manifest.write_text(encode({"manifest_id": unit.manifest_id, "source_revision": unit.revision,
            "evidence_manifest": str(evidence_manifest), "plan_catalog": str(catalog), "plan_count": len(plans),
            "next_unit": unit.index, "unit_pattern": "unit-{index:08d}.json", "receipt": unit.receipt}), encoding="utf-8")
        return manifest
