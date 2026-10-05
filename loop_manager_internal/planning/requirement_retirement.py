"""Final Requirement assessment inputs and Manager-owned byte-preserving moves.

This boundary uses confirmed catalog state and pinned Git blobs. It never
infers fulfillment from a plan graph or from an unmerged Worker candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from loop_manager_internal.storage.requirement_judgment_state import (
    Judgment, RequirementJudgmentRepository,
)


SPECIFICATIONS = ("docs/PRODUCT.md", "docs/ARCHITECTURE.md")

SOURCE = re.compile(r"docs/requirements/[^/\\]+\.md\Z")
ASSESSMENT = re.compile(r"^REQUIREMENT_JUDGMENTS:\s*(\[.*\])\s*$", re.MULTILINE)
AUDIT_ASSESSMENT = re.compile(r"^REQUIREMENT_AUDIT:\s*(\[.*\])\s*$", re.MULTILINE)


def _single_result(pattern: re.Pattern[str], text: str):
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise RetirementError("Requirement 판단 결과가 중복됐습니다.")
    return matches[0] if matches else None


class RetirementError(RuntimeError):
    """A final assessment or move lacks authoritative evidence."""


class RetirementReassessment(RetirementError):
    """A receipted move was withdrawn; resume ordinary Planner assessment."""


@dataclass(frozen=True)
class FinalInput:
    path: str
    token: str
    source_oid: str
    criteria: tuple[str, ...]
    related_ids: tuple[str, ...]


@dataclass(frozen=True)
class AuditInput:
    path: str
    token: str
    criteria: tuple[str, ...]
    related_ids: tuple[str, ...]


def audit_prompt(inputs: tuple[AuditInput, ...]) -> str:
    if not inputs:
        return ""
    payload = [
        {"path": item.path, "input_token": item.token,
         "related_plan_ids": item.related_ids, "criteria": item.criteria}
        for item in inputs
    ]
    return (
        "\n\n다음은 이미 archive된 과거 Requirement의 일회성 감사 입력이다. "
        "일반 활성 Requirement 입력으로 취급하거나 파일을 수정하지 마라. "
        "원래 전체 Acceptance Criteria와 실제 Merge된 구현·검증 근거를 대조하라. "
        "각 항목에 path, input_token, outcome(fulfilled|premature|superseded|uncertain), "
        "criteria_evidence(기준 순서대로 비어 있지 않은 근거), "
        "relevant_paths(검토한 구현·테스트 상대 경로), reason을 넣어 "
        "마지막 응답에 REQUIREMENT_AUDIT: JSON 배열을 한 줄로 적어라. "
        "premature는 현재도 필요한 기준이 미충족임이 증명될 때만, "
        "superseded는 제품 목표의 교체·철회 근거가 명확할 때만 사용하라. "
        "premature 항목의 후속 계획은 원문 복원이 원격 확인된 다음 활성 입력에서 작성하라. "
        "불확실하면 uncertain을 사용하라.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def assessment_prompt(inputs: tuple[FinalInput, ...]) -> str:
    if not inputs:
        return ""
    payload = [
        {"path": item.path, "input_token": item.token,
         "related_plan_ids": item.related_ids, "criteria": item.criteria}
        for item in inputs
    ]
    return (
        "\n\n아래 Requirement는 관련 ExecPlan이 모두 실제 MERGED다. 최신 원격 구현과 "
        "검증 근거를 읽고 각 Acceptance Criterion의 전체 충족 여부를 판단하라. "
        "계획 작성 완료만으로 충족을 추정하지 마라. 추가 작업은 일반 Planner 권한으로 "
        "ExecPlan에 연결하라. Requirement 파일은 수정하지 마라. "
        "마지막 응답에 REQUIREMENT_JUDGMENTS: JSON 배열을 정확히 한 줄로 적어라. "
        "각 항목의 필드는 path, input_token, outcome(fulfilled|unfulfilled|uncertain), "
        "criteria_evidence(입력 criteria와 같은 순서의 비어 있지 않은 문자열 배열), "
        "relevant_paths(판단에 사용한 구현·테스트 상대 경로 배열)다. "
        "PRODUCT·ARCHITECTURE 근거는 relevant_paths에 파일#정확한 Markdown 제목 줄로 "
        "관련 절을 모두 지정하라(예: docs/PRODUCT.md### 제품 규칙). "
        "절 지정은 해당 절과 하위 절을 포함한다. 문서 전체가 관련되면 파일 경로를 사용하고 관련 근거가 없는 문서는 생략하라. "
        "근거가 부족하면 uncertain을 사용하고 criteria_evidence에 사용자가 결정해야 할 "
        "구체적인 문제와 해당 Requirement에 명시해야 할 결정을 적어라.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _tree(git, repository: Path, revision: str, *paths: str) -> dict[str, tuple[str, str]]:
    raw = git.run_bytes(repository, "ls-tree", "-rz", "--full-tree", revision, "--", *paths)
    result = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        left, separator, path = record.partition(b"\t")
        fields = left.split()
        if not separator or len(fields) != 3:
            raise RetirementError("Requirement Git tree entry가 유효하지 않습니다.")
        try:
            name = path.decode("utf-8")
            mode, kind, oid = (field.decode("ascii") for field in fields)
        except UnicodeError as error:
            raise RetirementError("Requirement Git tree path를 읽을 수 없습니다.") from error
        if name in result:
            raise RetirementError(f"Requirement Git tree path가 중복됐습니다: {name}")
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise RetirementError(f"Git tree 항목이 일반 파일이 아닙니다: {name}")
        result[name] = (mode, oid)
    return result


def _sources(value: object, plan_id: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as error:
        raise RetirementError(f"ExecPlan {plan_id} source가 유효하지 않습니다.") from error
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise RetirementError(f"ExecPlan {plan_id} source가 유효하지 않습니다.")
    return tuple(parsed)


def _criteria(contents: bytes) -> tuple[str, ...]:
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise RetirementError("Requirement Acceptance Criteria가 UTF-8이 아닙니다.") from error
    active = False
    criteria = []
    for line in lines:
        if line.startswith("## "):
            if active:
                break
            active = line.strip().lower() in {"## acceptance criteria", "## 완료 조건"}
        elif active and re.match(r"^\s*-\s+\S", line):
            criteria.append(line.strip()[2:].strip())
    if not criteria:
        raise RetirementError("Requirement에 확인 가능한 Acceptance Criteria 목록이 없습니다.")
    return tuple(criteria)


def _safe_parent(worktree: Path, relative_path: str, *, create: bool = False) -> None:
    current = worktree
    for part in PurePosixPath(relative_path).parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not create:
                return
            current.mkdir()
            mode = current.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise RetirementError(f"Requirement 이동 경로의 부모가 디렉터리가 아닙니다: {relative_path}")


def _valid_evidence_path(path: str) -> bool:
    if (not path or len(path) > 400 or path.startswith("/") or "\\" in path
        or any(ord(character) < 32 for character in path)):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


class RequirementRetirementPort:
    def __init__(self, connection: sqlite3.Connection, git):
        self.connection = connection
        self.git = git
        self.judgments = RequirementJudgmentRepository(connection)

    def _inputs(self, repo: str, repository: Path, revision: str):
        tree = _tree(
            self.git, repository, revision, "docs/requirements", "docs/PRODUCT.md",
            "docs/ARCHITECTURE.md",
        )
        rows = list(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? ORDER BY plan_id", (repo,),
        ))
        cancellations = list(self.connection.execute(
            "SELECT plan_id, sources FROM execplan_cancellations WHERE repo = ?", (repo,),
        ))
        drafts = list(self.connection.execute(
            "SELECT target_path FROM document_drafts WHERE repo = ? "
            "AND state IN ('pending', 'approved')", (repo,),
        ))
        draft_paths = {str(row["target_path"]) for row in drafts}
        for path, (mode, oid) in sorted(tree.items()):
            if not SOURCE.fullmatch(path):
                continue
            if mode not in {"100644", "100755"}:
                raise RetirementError(f"Requirement가 일반 파일이 아닙니다: {path}")
            related = [row for row in rows if path in _sources(row["sources"], str(row["plan_id"]))]
            canceled = [row for row in cancellations if path in _sources(row["sources"], str(row["plan_id"]))]
            if canceled or not related or any(str(row["status"]) != "MERGED" for row in related):
                continue
            if path in draft_paths or "docs/PRODUCT.md" in draft_paths or "docs/ARCHITECTURE.md" in draft_paths:
                continue
            destination = "docs/requirements/archive/" + path.removeprefix("docs/requirements/")
            if destination in tree:
                # The audit records this active/archive collision as uncertain.
                # Other Requirements remain independently eligible.
                continue
            yield path, oid, related, tree

    def _evidence(self, repository: Path, revision: str,
                  paths: tuple[str, ...]) -> dict[str, object]:
        files = tuple(path.split("#", 1)[0] for path in paths)
        tree = _tree(self.git, repository, revision, *files) if files else {}
        result = {}
        for path in paths:
            filename, separator, heading = path.partition("#")
            entry = tree.get(filename)
            if not separator:
                result[path] = entry
                continue
            if filename not in SPECIFICATIONS or not re.fullmatch(r"#{1,6} .+", heading):
                raise RetirementError(f"명세 근거 절이 유효하지 않습니다: {path}")
            if entry is None or entry[0] == "120000":
                result[path] = None
                continue
            try:
                lines = self.git.run_bytes(
                    repository, "cat-file", "blob", entry[1],
                ).decode("utf-8").splitlines()
            except UnicodeError as error:
                raise RetirementError(f"명세 근거가 UTF-8이 아닙니다: {path}") from error
            matches = [index for index, line in enumerate(lines) if line == heading]
            if len(matches) != 1:
                result[path] = None
                continue
            start = matches[0]
            level = len(heading.split(" ", 1)[0])
            end = next((index for index in range(start + 1, len(lines))
                        if re.match(r"^#{1," + str(level) + r"} ", lines[index])), len(lines))
            result[path] = (entry[0], _digest(lines[start:end]))
        return result

    def _token(self, repo: str, repository: Path, revision: str, path: str,
               oid: str, related, tree, relevant_paths: tuple[str, ...]) -> str:
        if any(not _valid_evidence_path(item) for item in relevant_paths):
            raise RetirementError(f"판단 관련 경로가 유효하지 않습니다: {path}")
        relevant = self._evidence(repository, revision, relevant_paths)
        plan_evidence = tuple(
            (
                str(row["plan_id"]), str(row["path"]), str(row["sources"]),
                str(row["content_hash"]), str(row["status"]),
                str(row["pr_head_oid"] or ""), str(row["publication_remote_oid"] or ""),
                str(row["verify_token"] or ""), str(row["review_token"] or ""),
                _digest((str(row["verify_result"] or ""), str(row["review_result"] or ""))),
            ) for row in related
        )
        return _digest(("explicit-specification-evidence-v2", path, oid, plan_evidence,
                        tuple(sorted((item, relevant.get(item)) for item in relevant_paths))))

    def pending(self, repo: str, repository: Path, revision: str) -> tuple[FinalInput, ...]:
        result = []
        for path, oid, related, tree in self._inputs(repo, repository, revision):
            prior = self.judgments.get(repo, path)
            relevant_paths = prior.relevant_paths if prior else ()
            token = self._token(repo, repository, revision, path, oid, related, tree, relevant_paths)
            if prior is not None and prior.input_token == token and prior.invalidation_reason is None:
                continue
            if prior is not None and prior.input_token != token:
                self.judgments.invalidate(repo, path, "related specification, plan, implementation or validation changed")
            contents = self.git.run_bytes(repository, "cat-file", "blob", oid)
            try:
                criteria = _criteria(contents)
            except RetirementError as error:
                self.judgments.put(repo, Judgment(
                    path, token, "uncertain", (str(error),), relevant_paths, revision,
                ))
                continue
            result.append(FinalInput(path, token, oid, criteria,
                                     tuple(str(row["plan_id"]) for row in related)))
        return tuple(result)

    def validate_result(self, repo: str, repository: Path, revision: str,
                        planner_text: str) -> tuple[Judgment, ...]:
        expected = {item.path: item for item in self.pending(repo, repository, revision)}
        match = _single_result(ASSESSMENT, planner_text)
        if not expected:
            if match is None:
                return ()
            try:
                repeated = json.loads(match.group(1))
            except ValueError as error:
                raise RetirementError("Requirement 판단 JSON이 유효하지 않습니다.") from error
            if not isinstance(repeated, list):
                raise RetirementError("Requirement 판단 JSON 배열이 필요합니다.")
            preserved = []
            for value in repeated:
                if not isinstance(value, dict) or not isinstance(value.get("path"), str):
                    raise RetirementError("보존된 Requirement 판단이 유효하지 않습니다.")
                paths = value.get("relevant_paths")
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                    raise RetirementError("보존된 Requirement 판단 경로가 유효하지 않습니다.")
                prior = self.judgments.get(repo, value["path"])
                current = next((entry for entry in self._inputs(repo, repository, revision)
                                if entry[0] == value["path"]), None)
                if (prior is None or current is None or prior.invalidation_reason is not None
                    or prior.input_token != self._token(
                        repo, repository, revision, current[0], current[1],
                        current[2], current[3], prior.relevant_paths,
                    )
                    or prior.outcome != value.get("outcome")
                    or list(prior.criteria_evidence) != value.get("criteria_evidence")
                    or list(prior.relevant_paths) != sorted(set(paths))):
                    raise RetirementError("보존된 Requirement 판단이 현재 입력과 다릅니다.")
                preserved.append(prior)
            return tuple(preserved)
        if match is None:
            raise RetirementError("일반 Planner의 Requirement 최종 판단이 없습니다.")
        try:
            values = json.loads(match.group(1))
        except ValueError as error:
            raise RetirementError("Requirement 판단 JSON이 유효하지 않습니다.") from error
        if not isinstance(values, list) or len(values) != len(expected):
            raise RetirementError("Requirement 판단 대상 집합이 입력과 다릅니다.")
        judgments = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {
                "path", "input_token", "outcome", "criteria_evidence", "relevant_paths",
            }:
                raise RetirementError("Requirement 판단 필드가 유효하지 않습니다.")
            path = value["path"]
            if path not in expected or any(item.source_path == path for item in judgments):
                raise RetirementError("Requirement 판단 path가 입력과 다릅니다.")
            item = expected[path]
            if value["input_token"] != item.token or value["outcome"] not in {
                "fulfilled", "unfulfilled", "uncertain",
            }:
                raise RetirementError(f"Requirement 판단 입력 또는 결과가 유효하지 않습니다: {path}")
            evidence = value["criteria_evidence"]
            paths = value["relevant_paths"]
            if (not isinstance(evidence, list) or len(evidence) != len(item.criteria)
                or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 4000
                       for entry in evidence)
                or not isinstance(paths, list) or len(paths) > 200
                or any(not isinstance(entry, str) or not _valid_evidence_path(entry)
                       for entry in paths)
                or (value["outcome"] != "uncertain" and not paths)):
                raise RetirementError(f"Requirement 기준별 근거가 불완전합니다: {path}")
            relevant_paths = tuple(sorted(set(paths)))
            current = next(
                (entry for entry in self._inputs(repo, repository, revision) if entry[0] == path),
                None,
            )
            if current is None:
                raise RetirementError(f"Requirement 최종 판단 자격이 변경됐습니다: {path}")
            if value["outcome"] == "fulfilled":
                observed = self._evidence(repository, revision, relevant_paths)
                if any(not observed.get(entry) or observed[entry][0] == "120000"
                       for entry in relevant_paths):
                    raise RetirementError(f"충족 판단 근거 파일이 없습니다: {path}")
            token = self._token(repo, repository, revision, path, item.source_oid,
                                current[2], current[3], relevant_paths)
            judgments.append(Judgment(path, token, value["outcome"],
                                      tuple(evidence), relevant_paths, revision))
        return tuple(judgments)

    def record(self, repo: str, judgments: tuple[Judgment, ...]) -> None:
        for judgment in judgments:
            self.judgments.put(repo, judgment)

    def fulfilled_moves(self, repo: str, repository: Path, revision: str) -> tuple[str, ...]:
        moves = []
        for path, oid, related, tree in self._inputs(repo, repository, revision):
            judgment = self.judgments.get(repo, path)
            if judgment is None or judgment.outcome != "fulfilled" or judgment.invalidation_reason:
                continue
            if judgment.input_token == self._token(
                repo, repository, revision, path, oid, related, tree, judgment.relevant_paths,
            ):
                moves.append(path)
        return tuple(moves)

    def audit_restorations(self, repo: str, repository: Path,
                           revision: str) -> tuple[str, ...]:
        """Pin historical archive evidence; restore only proven live unfinished goals."""
        tree = _tree(self.git, repository, revision, "docs/requirements",
                     "docs/PRODUCT.md", "docs/ARCHITECTURE.md")
        rows = list(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ?", (repo,),
        ))
        canceled = list(self.connection.execute(
            "SELECT plan_id, sources FROM execplan_cancellations WHERE repo = ?", (repo,),
        ))
        restorations = []
        observed_sources = set()
        for archived, (mode, oid) in sorted(tree.items()):
            if not archived.startswith("docs/requirements/archive/") or archived.count("/") != 3 or not archived.endswith(".md"):
                continue
            source = "docs/requirements/" + archived.removeprefix("docs/requirements/archive/")
            if not SOURCE.fullmatch(source):
                continue
            managed_move = self.connection.execute(
                "SELECT source_oid FROM requirement_move_receipts "
                "WHERE repo = ? AND source_path = ? AND stage IN ('pending', 'published')",
                (repo, source),
            ).fetchone()
            if managed_move is not None and str(managed_move["source_oid"]) == oid:
                continue
            observed_sources.add(source)
            related = [row for row in rows if source in _sources(row["sources"], str(row["plan_id"]))]
            cancelled = [row for row in canceled if source in _sources(row["sources"], str(row["plan_id"]))]
            previous = self.connection.execute(
                "SELECT * FROM requirement_audit WHERE repo = ? AND source_path = ?",
                (repo, source),
            ).fetchone()
            relevant = (tuple(json.loads(str(previous["relevant_paths"])))
                        if previous is not None else ())
            token = _digest((mode, tree.get(source),
                             tuple(sorted(str(row["plan_id"]) for row in cancelled)),
                             self._token(repo, repository, revision, source, oid,
                                         related, tree, relevant)))
            if previous is not None and previous["input_token"] == token:
                if previous["outcome"] == "restore" and previous["stage"] != "published":
                    restorations.append(source)
                continue
            if mode not in {"100644", "100755"} or source in tree:
                outcome, reason = "uncertain", "historical archive mode or active-path collision"
            elif cancelled:
                outcome, reason = "uncertain", "cancelled source relationship is not Merge proof"
            elif any(str(row["status"]) != "MERGED" for row in related):
                outcome, reason = "restore", "live unfinished source-linked ExecPlan"
            elif related:
                outcome, reason = "pending", "historical full-Requirement judgment is absent"
            else:
                outcome, reason = "uncertain", "historical source relationship is absent"
            criteria = ()
            if outcome == "pending":
                contents = self.git.run_bytes(repository, "cat-file", "blob", oid)
                try:
                    criteria = _criteria(contents)
                except RetirementError as error:
                    outcome, reason = "uncertain", str(error)
            self.connection.execute(
                "INSERT INTO requirement_audit "
                "(repo, source_path, input_token, source_oid, observed_sha, outcome, reason, criteria, "
                "criteria_evidence, relevant_paths, stage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, 'observed') "
                "ON CONFLICT(repo, source_path) DO UPDATE SET "
                "input_token=excluded.input_token, source_oid=excluded.source_oid, "
                "observed_sha=excluded.observed_sha, outcome=excluded.outcome, "
                "reason=excluded.reason, criteria=excluded.criteria, "
                "criteria_evidence='[]', relevant_paths=excluded.relevant_paths, "
                "stage='observed', updated_at=CURRENT_TIMESTAMP",
                (repo, source, token, oid, revision, outcome, reason,
                 json.dumps(criteria, ensure_ascii=False), json.dumps(relevant)),
            )
            if outcome == "restore":
                self.judgments.invalidate(repo, source, "historical archive requires restoration")
                restorations.append(source)
        for row in list(self.connection.execute(
            "SELECT source_path, source_oid FROM requirement_audit "
            "WHERE repo = ? AND stage = 'observed'",
            (repo,),
        )):
            source = str(row["source_path"])
            if source not in observed_sources and source in tree:
                if tree[source] == ("100644", str(row["source_oid"])) or tree[source] == (
                    "100755", str(row["source_oid"]),
                ):
                    self.connection.execute(
                        "UPDATE requirement_audit SET stage = 'published', "
                        "reason = 'original bytes restored on remote', "
                        "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND source_path = ?",
                        (repo, source),
                    )
                else:
                    self.connection.execute(
                        "UPDATE requirement_audit SET outcome = 'uncertain', "
                        "reason = 'restored active content differs from archived evidence', "
                        "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND source_path = ?",
                        (repo, source),
                    )
        return tuple(restorations)

    def audit_pending(self, repo: str, repository: Path, revision: str) -> tuple[AuditInput, ...]:
        self.audit_restorations(repo, repository, revision)
        rows = self.connection.execute(
            "SELECT source_path, input_token, criteria FROM requirement_audit "
            "WHERE repo = ? AND outcome = 'pending' ORDER BY source_path LIMIT 8", (repo,),
        )
        plans = list(self.connection.execute(
            "SELECT plan_id, sources FROM exec_plans WHERE repo = ?", (repo,),
        ))
        return tuple(AuditInput(
            str(row["source_path"]), str(row["input_token"]),
            tuple(json.loads(str(row["criteria"]))),
            tuple(str(plan["plan_id"]) for plan in plans if str(row["source_path"]) in
                  _sources(plan["sources"], str(plan["plan_id"]))),
        ) for row in rows)

    def record_audit_result(self, repo: str, repository: Path, revision: str,
                            planner_text: str) -> None:
        pending = {item.path: item for item in self.audit_pending(repo, repository, revision)}
        match = _single_result(AUDIT_ASSESSMENT, planner_text)
        if not pending:
            if match is not None:
                try:
                    repeated = json.loads(match.group(1))
                except ValueError as error:
                    raise RetirementError("과거 Requirement 감사 JSON이 유효하지 않습니다.") from error
                if not isinstance(repeated, list):
                    raise RetirementError("과거 Requirement 감사 JSON 배열이 필요합니다.")
                for value in repeated:
                    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
                        raise RetirementError("보존된 과거 Requirement 감사가 유효하지 않습니다.")
                    paths = value.get("relevant_paths")
                    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                        raise RetirementError("보존된 과거 Requirement 감사 경로가 유효하지 않습니다.")
                    row = self.connection.execute(
                        "SELECT * FROM requirement_audit WHERE repo = ? AND source_path = ?",
                        (repo, value["path"]),
                    ).fetchone()
                    mapped = {"premature": "restore", "fulfilled": "fulfilled",
                              "superseded": "superseded", "uncertain": "uncertain"}
                    if (row is None
                        or mapped.get(value.get("outcome")) != row["outcome"]
                        or value.get("reason") != row["reason"]
                        or value.get("criteria_evidence") != json.loads(str(row["criteria_evidence"]))
                        or sorted(set(paths)) != json.loads(str(row["relevant_paths"]))):
                        raise RetirementError("보존된 과거 Requirement 감사가 현재 입력과 다릅니다.")
            return
        if match is None:
            raise RetirementError("일반 Planner의 과거 Requirement 감사 결과가 없습니다.")
        try:
            values = json.loads(match.group(1))
        except ValueError as error:
            raise RetirementError("과거 Requirement 감사 JSON이 유효하지 않습니다.") from error
        if not isinstance(values, list) or len(values) != len(pending):
            raise RetirementError("과거 Requirement 감사 대상 집합이 입력과 다릅니다.")
        tree = _tree(self.git, repository, revision, "docs/requirements",
                     "docs/PRODUCT.md", "docs/ARCHITECTURE.md")
        plans = list(self.connection.execute("SELECT * FROM exec_plans WHERE repo = ?", (repo,)))
        updates = []
        seen = set()
        for value in values:
            if not isinstance(value, dict) or set(value) != {
                "path", "input_token", "outcome", "criteria_evidence", "relevant_paths", "reason",
            }:
                raise RetirementError("과거 Requirement 감사 필드가 유효하지 않습니다.")
            path = value["path"]
            if not isinstance(path, str) or path not in pending or path in seen:
                raise RetirementError("과거 Requirement 감사 path가 입력과 다릅니다.")
            seen.add(path)
            item = pending[path]
            evidence, paths, reason = value["criteria_evidence"], value["relevant_paths"], value["reason"]
            if (value["input_token"] != item.token
                or value["outcome"] not in {"fulfilled", "premature", "superseded", "uncertain"}
                or not isinstance(evidence, list) or len(evidence) != len(item.criteria)
                or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 4000
                       for entry in evidence)
                or not isinstance(paths, list) or len(paths) > 200
                or any(not isinstance(entry, str) or not _valid_evidence_path(entry)
                       for entry in paths)
                or (value["outcome"] != "uncertain" and not paths)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000):
                raise RetirementError(f"과거 Requirement 감사 근거가 불완전합니다: {path}")
            archived = "docs/requirements/archive/" + path.removeprefix("docs/requirements/")
            if archived not in tree or path in tree:
                raise RetirementError(f"과거 Requirement 감사 원본이 변경됐습니다: {path}")
            related = [row for row in plans if path in _sources(row["sources"], str(row["plan_id"]))]
            relevant = tuple(sorted(set(paths)))
            if value["outcome"] in {"fulfilled", "superseded"}:
                observed = self._evidence(repository, revision, relevant)
                if any(not observed.get(entry) or observed[entry][0] == "120000"
                       for entry in relevant):
                    raise RetirementError(f"과거 Requirement 완료·교체 근거 파일이 없습니다: {path}")
            token = _digest((tree[archived][0], tree.get(path), (),
                             self._token(repo, repository, revision, path, tree[archived][1],
                                         related, tree, relevant)))
            updates.append((path, token, value["outcome"], tuple(evidence), relevant, reason))
        for path, token, outcome, evidence, relevant, reason in updates:
            self.connection.execute(
                "UPDATE requirement_audit SET input_token = ?, outcome = ?, reason = ?, "
                "criteria_evidence = ?, relevant_paths = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND source_path = ?",
                (token, "restore" if outcome == "premature" else outcome, reason,
                 json.dumps(evidence, ensure_ascii=False), json.dumps(relevant), repo, path),
            )
            if outcome == "premature":
                self.judgments.invalidate(repo, path, "historical audit found unfinished criteria")

    def stage_restorations(self, repo: str, repository: Path, revision: str,
                           worktree: Path) -> tuple[str, ...]:
        paths = []
        candidates = self.audit_restorations(repo, repository, revision)
        for source in candidates:
            row = self.connection.execute(
                "SELECT input_token, source_oid, stage FROM requirement_audit "
                "WHERE repo = ? AND source_path = ?",
                (repo, source),
            ).fetchone()
            assert row is not None
            archived = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
            origin, target = worktree / archived, worktree / source
            _safe_parent(worktree, archived)
            _safe_parent(worktree, source, create=True)
            original = self.git.run_bytes(repository, "cat-file", "blob", str(row["source_oid"]))
            if row["stage"] == "staged" and not origin.exists():
                if target.is_symlink() or not target.is_file() or target.read_bytes() != original:
                    raise RetirementError(f"Requirement 복원 영수증과 내용이 다릅니다: {source}")
                self.connection.execute(
                    "UPDATE requirement_audit SET observed_sha = ?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND source_path = ?", (revision, repo, source),
                )
                paths.extend((archived, source))
                continue
            if (origin.is_symlink() or not origin.is_file() or origin.read_bytes() != original
                or target.exists() or target.is_symlink()):
                raise RetirementError(f"Requirement 복원 원본 또는 목적지가 안전하지 않습니다: {source}")
            changed = self.connection.execute(
                "UPDATE requirement_audit SET stage = 'staged', observed_sha = ?, updated_at=CURRENT_TIMESTAMP "
                "WHERE repo = ? AND source_path = ? AND input_token = ?",
                (revision, repo, source, row["input_token"]),
            )
            if changed.rowcount != 1:
                raise RetirementError(f"Requirement 복원 입력이 변경됐습니다: {source}")
            os.rename(origin, target)
            if target.read_bytes() != original:
                raise RetirementError(f"Requirement 복원 중 원문이 변경됐습니다: {source}")
            paths.extend((archived, source))
        return tuple(paths)

    def stage_moves(self, repo: str, repository: Path, revision: str,
                    worktree: Path, *, excluded_sources: tuple[str, ...] = ()) -> tuple[str, ...]:
        paths = []
        eligible = {path: oid for path, oid, _rows, _tree in self._inputs(repo, repository, revision)}
        for source in self.fulfilled_moves(repo, repository, revision):
            if source in excluded_sources:
                continue
            destination = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
            origin = worktree / source
            target = worktree / destination
            _safe_parent(worktree, source)
            _safe_parent(worktree, destination, create=True)
            original = self.git.run_bytes(repository, "cat-file", "blob", eligible[source])
            receipt = self.connection.execute(
                "SELECT base_sha, source_oid, judgment_token, stage FROM requirement_move_receipts "
                "WHERE repo = ? AND source_path = ?", (repo, source),
            ).fetchone()
            judgment = self.judgments.get(repo, source)
            assert judgment is not None
            if receipt is not None and (
                str(receipt["source_oid"]),
                str(receipt["judgment_token"]),
            ) != (
                eligible[source], judgment.input_token,
            ):
                raise RetirementError(f"Requirement 이동 영수증의 입력이 변경됐습니다: {source}")
            if not origin.exists() and receipt is not None and receipt["stage"] == "pending":
                if target.is_symlink() or not target.is_file() or target.read_bytes() != original:
                    raise RetirementError(f"Requirement 이동 재개 내용이 유효하지 않습니다: {source}")
                self.connection.execute(
                    "UPDATE requirement_move_receipts SET base_sha = ?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND source_path = ?", (revision, repo, source),
                )
                paths.extend((source, destination))
                continue
            try:
                mode = origin.lstat().st_mode
            except FileNotFoundError as error:
                raise RetirementError(f"Requirement 원본이 없습니다: {source}") from error
            if not stat.S_ISREG(mode) or target.exists() or target.is_symlink():
                raise RetirementError(f"Requirement 이동 원본 또는 목적지가 안전하지 않습니다: {source}")
            if origin.read_bytes() != original:
                raise RetirementError(f"Requirement 원본 내용이 변경됐습니다: {source}")
            self.connection.execute(
                "INSERT INTO requirement_move_receipts "
                "(repo, source_path, base_sha, source_oid, judgment_token, stage) "
                "VALUES (?, ?, ?, ?, ?, 'pending') "
                "ON CONFLICT(repo, source_path) DO UPDATE SET "
                "base_sha=excluded.base_sha, source_oid=excluded.source_oid, "
                "judgment_token=excluded.judgment_token, "
                "stage='pending', updated_at=CURRENT_TIMESTAMP",
                (repo, source, revision, eligible[source], judgment.input_token),
            )
            os.rename(origin, target)
            if target.read_bytes() != original:
                raise RetirementError(f"Requirement 이동 내용이 변경됐습니다: {source}")
            paths.extend((source, destination))
        return tuple(paths)

    def _withdraw_move(self, repo: str, repository: Path, revision: str,
                       worktree: Path, row) -> None:
        source = str(row["source_path"])
        destination = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
        _safe_parent(worktree, source)
        _safe_parent(worktree, destination)
        remote = _tree(self.git, repository, revision, source, destination)
        if (destination in remote or source not in remote
                or remote[source][0] not in {"100644", "100755"}):
            raise RetirementError(f"Requirement 이동 철회 원격 경로가 안전하지 않습니다: {source}")
        original = self.git.run_bytes(repository, "cat-file", "blob", str(row["source_oid"]))
        current = self.git.run_bytes(repository, "cat-file", "blob", remote[source][1])
        origin, target = worktree / source, worktree / destination
        if origin.is_symlink() or target.is_symlink():
            raise RetirementError(f"Requirement 이동 철회 경로가 symlink입니다: {source}")
        if origin.exists():
            if not origin.is_file() or origin.read_bytes() != current or target.exists():
                raise RetirementError(f"Requirement 이동 철회 원본에 보존할 변경이 있습니다: {source}")
        else:
            if not target.is_file() or target.read_bytes() != original or original != current:
                raise RetirementError(f"Requirement 이동 철회 영수증과 내용이 다릅니다: {source}")
            os.rename(target, origin)
        # Reset only the proven Manager move's index entries; a publication
        # interruption may have staged them even when HEAD still equals base.
        self.git.run(worktree, "reset", revision, "--", source, destination)
        # An interrupted inverse rename is recognized above before retiring its
        # pending receipt. Unrelated worktree files and judgment history survive.
        self.judgments.invalidate(repo, source, "staged retirement withdrawn after related input change")
        self.connection.execute(
            "DELETE FROM requirement_move_receipts WHERE repo = ? AND source_path = ? AND stage = 'pending'",
            (repo, source),
        )

    def receipted_paths(self, repo: str, repository: Path, revision: str,
                        worktree: Path) -> tuple[str, ...]:
        paths = []
        currently_fulfilled = set(self.fulfilled_moves(repo, repository, revision))
        for row in self.connection.execute(
            "SELECT source_path, source_oid, judgment_token FROM requirement_move_receipts "
            "WHERE repo = ? AND stage = 'pending'",
            (repo,),
        ):
            source = str(row["source_path"])
            judgment = self.judgments.get(repo, source)
            if (source not in currently_fulfilled or judgment is None
                or judgment.input_token != str(row["judgment_token"])):
                self._withdraw_move(repo, repository, revision, worktree, row)
                raise RetirementReassessment(
                    f"Requirement 이동을 철회했습니다. 최신 관련 계획·근거로 일반 Planner 판단을 갱신하세요: {source}"
                )
            destination = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
            origin, target = worktree / source, worktree / destination
            _safe_parent(worktree, source)
            _safe_parent(worktree, destination)
            if origin.exists() and not target.exists():
                continue  # Interrupted before the Manager's rename.
            original = self.git.run_bytes(repository, "cat-file", "blob", str(row["source_oid"]))
            if (origin.exists() or target.is_symlink() or not target.is_file()
                    or target.read_bytes() != original):
                raise RetirementError(f"Requirement 이동 영수증과 worktree가 다릅니다: {source}")
            paths.extend((source, destination))
        valid_restorations = set(self.audit_restorations(repo, repository, revision))
        for row in self.connection.execute(
            "SELECT source_path, source_oid FROM requirement_audit "
            "WHERE repo = ? AND outcome = 'restore' AND stage = 'staged'",
            (repo,),
        ):
            source = str(row["source_path"])
            if source not in valid_restorations:
                raise RetirementError(f"Requirement 복원 입력이 변경됐습니다: {source}")
            archived = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
            origin, target = worktree / archived, worktree / source
            _safe_parent(worktree, archived)
            _safe_parent(worktree, source)
            if origin.exists() and not target.exists():
                continue
            original = self.git.run_bytes(repository, "cat-file", "blob", str(row["source_oid"]))
            if (origin.exists() or target.is_symlink() or not target.is_file()
                    or target.read_bytes() != original):
                raise RetirementError(f"Requirement 복원 영수증과 worktree가 다릅니다: {source}")
            paths.extend((archived, source))
        return tuple(paths)
