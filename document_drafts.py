"""One-time self-assessment document drafts and exact user decisions."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from uuid import uuid4

import execplan_catalog
import maintenance_state


class DocumentDraftError(ValueError):
    pass


R027_SOURCE = (
    "docs/requirements/[R-027] Shared Quality Guidance and Full Project "
    "Refactoring Assessment.md"
)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True,
    )
    if result.returncode:
        raise DocumentDraftError("문서의 원격 Git revision을 확인할 수 없습니다.")
    return result.stdout


def remote_revision(repository: Path, base_branch: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch) or ".." in base_branch:
        raise DocumentDraftError("기준 branch가 유효하지 않습니다.")
    return _git(
        repository, "rev-parse", "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).decode("ascii").strip()


def document_at(repository: Path, revision: str, path: str) -> tuple[str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise DocumentDraftError("원본 revision이 유효하지 않습니다.")
    if not _document_path(path):
        raise DocumentDraftError("활성 제품 문서 경로가 아닙니다.")
    content = _git(repository, "show", f"{revision}:{path}")
    changed_at = _git(
        repository, "log", "-1", "--format=%H", revision, "--", path,
    ).decode("ascii").strip()
    if not changed_at:
        raise DocumentDraftError("문서의 원본 revision을 확인할 수 없습니다.")
    return changed_at, content


def _document_path(path: str) -> bool:
    if path in {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}:
        return True
    return bool(re.fullmatch(r"docs/requirements/[^/]+\.md", path))


def source_relationships(
    connection: sqlite3.Connection, repo: str, path: str,
    *, exclude_plan_id: str | None = None,
) -> str:
    """Snapshot nonterminal plan IDs and their source lists for one document."""
    relationships = []
    rows = connection.execute(
        "SELECT plan_id, sources FROM exec_plans "
        "WHERE repo = ? AND status != 'MERGED' ORDER BY plan_id", (repo,),
    )
    for row in rows:
        try:
            sources = json.loads(row["sources"])
        except (TypeError, json.JSONDecodeError) as error:
            raise DocumentDraftError("비종료 ExecPlan source 관계가 유효하지 않습니다.") from error
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise DocumentDraftError("비종료 ExecPlan source 관계가 유효하지 않습니다.")
        if path in sources and row["plan_id"] != exclude_plan_id:
            relationships.append([str(row["plan_id"]), sorted(sources)])
    return json.dumps(relationships, ensure_ascii=False, separators=(",", ":"))


def checked_source_relationships(
    connection: sqlite3.Connection, repo: str, repository: Path,
    revision: str, path: str, *, exclude_plan_id: str | None = None,
) -> str:
    """Require the fetched remote catalog to agree with nonterminal SQLite rows."""
    rows = connection.execute(
        "SELECT plan_id, sources FROM exec_plans WHERE repo = ? AND status != 'MERGED'",
        (repo,),
    )
    expected = {str(row["plan_id"]): tuple(sorted(json.loads(row["sources"])))
                for row in rows}
    remote: dict[str, tuple[str, ...]] = {}
    raw_paths = _git(
        repository, "ls-tree", "-r", "-z", "--name-only", revision,
        "--", "docs/plans",
    )
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        try:
            plan_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentDraftError("원격 ExecPlan 경로가 UTF-8이 아닙니다.") from error
        if not re.fullmatch(r"docs/plans/[^/]+\.md", plan_path):
            continue
        try:
            values = execplan_catalog.parse_execplan_text(
                plan_path, _git(repository, "show", f"{revision}:{plan_path}").decode("utf-8"),
                validate_sources=False,
            )
        except (RuntimeError, UnicodeDecodeError) as error:
            raise DocumentDraftError("원격 ExecPlan catalog를 안전하게 읽을 수 없습니다.") from error
        plan_id = str(values["id"])
        if plan_id in remote:
            raise DocumentDraftError("원격 ExecPlan ID가 중복됐습니다.")
        remote[plan_id] = tuple(sorted(values["sources"]))
    if any(plan_id not in remote or remote[plan_id] != sources
           for plan_id, sources in expected.items()):
        raise DocumentDraftError("원격과 SQLite의 비종료 ExecPlan source 관계가 다릅니다.")
    if any(plan_id not in expected for plan_id in remote):
        # Active MERGED plans can remain on the base until their archive pass.
        merged = {str(row[0]) for row in connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? AND status = 'MERGED'", (repo,),
        )}
        if any(plan_id not in merged for plan_id in remote if plan_id not in expected):
            raise DocumentDraftError("원격에 SQLite가 알지 못하는 비종료 ExecPlan이 있습니다.")
    return source_relationships(
        connection, repo, path, exclude_plan_id=exclude_plan_id,
    )


def _complete_diff(path: str, original: bytes, proposed: bytes) -> str:
    lines = difflib.unified_diff(
        original.decode("utf-8").splitlines(keepends=True),
        proposed.decode("utf-8").splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )


def ownership_clear(connection: sqlite3.Connection, repo: str) -> None:
    project = connection.execute(
        "SELECT phase, planning_workspace_owned FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if project is None:
        raise DocumentDraftError("등록된 프로젝트가 아닙니다.")
    if project["phase"] is not None or project["planning_workspace_owned"]:
        raise DocumentDraftError("프로젝트 작업 공간이 사용 중입니다.")
    occupied = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status IN "
        "('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING','PR_OPEN','PLAN_BLOCKED') LIMIT 1",
        (repo,),
    ).fetchone()
    if occupied is not None:
        raise DocumentDraftError("활성 ExecPlan 또는 대기 PR·차단 계획이 있습니다.")
    blocked = connection.execute(
        "SELECT 1 FROM project_blocks WHERE repo = ? LIMIT 1", (repo,),
    ).fetchone()
    if blocked is not None:
        raise DocumentDraftError("프로젝트 차단 상태입니다.")
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        raise DocumentDraftError("활성 유지보수 주기가 있습니다.")


@dataclass(frozen=True)
class DocumentDraft:
    draft_id: str
    repo: str
    finding_ids: tuple[str, ...]
    target_path: str
    assessment_revision: str
    original_revision: str
    original_hash: str
    original_content: bytes
    proposed_hash: str
    proposed_content: bytes
    complete_diff: str
    source_relationships: str
    meaning_evidence: str
    state: str
    requested_decision: str | None
    decision_revision: str | None
    decision_reason: str | None
    plan_id: str | None
    publication_error: str | None


class DocumentDraftRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        maintenance_state.ensure_schema(connection)

    def _assessment(self, repo: str) -> dict:
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(repo)
        if not cycle.full_assessment_completed or not cycle.full_assessment_result:
            raise DocumentDraftError("완료된 일회성 전수 평가가 없습니다.")
        report = json.loads(cycle.full_assessment_result)
        if report.get("source_revision") != cycle.full_assessment_source_sha:
            raise DocumentDraftError("전수 평가 source identity가 다릅니다.")
        return report

    def candidates(self, repo: str) -> dict[str, tuple[str, ...]]:
        report = self._assessment(repo)
        candidates: dict[str, list[str]] = {}
        for finding in report["findings"]:
            if finding["kind"] == "document" and finding["status"] == "candidate":
                path = finding["path"]
                if not _document_path(path):
                    raise DocumentDraftError("평가에 활성 문서 외의 후보가 있습니다.")
                candidates.setdefault(path, []).append(finding["id"])
        return {path: tuple(sorted(ids)) for path, ids in sorted(candidates.items())}

    def get(self, draft_id: str) -> DocumentDraft:
        row = self.connection.execute(
            "SELECT * FROM document_drafts WHERE draft_id = ?", (draft_id,),
        ).fetchone()
        if row is None:
            raise DocumentDraftError("문서 수정본 ID를 찾을 수 없습니다.")
        draft = DocumentDraft(
            draft_id=row["draft_id"], repo=row["repo"],
            finding_ids=tuple(json.loads(row["finding_ids"])),
            target_path=row["target_path"],
            assessment_revision=row["assessment_revision"],
            original_revision=row["original_revision"],
            original_hash=row["original_hash"],
            original_content=bytes(row["original_content"]),
            proposed_hash=row["proposed_hash"],
            proposed_content=bytes(row["proposed_content"]),
            complete_diff=row["complete_diff"],
            source_relationships=row["source_relationships"],
            meaning_evidence=row["meaning_evidence"], state=row["state"],
            requested_decision=row["requested_decision"],
            decision_revision=row["decision_revision"],
            decision_reason=row["decision_reason"],
            plan_id=row["plan_id"], publication_error=row["publication_error"],
        )
        try:
            intact = (
                _hash(draft.original_content) == draft.original_hash
                and _hash(draft.proposed_content) == draft.proposed_hash
                and _complete_diff(draft.target_path, draft.original_content,
                                   draft.proposed_content) == draft.complete_diff
            )
        except UnicodeDecodeError:
            intact = False
        if not intact or not _document_path(draft.target_path):
            raise DocumentDraftError("저장된 문서 수정본 바이트 또는 diff가 손상됐습니다.")
        return draft

    def list(self, repo: str) -> tuple[DocumentDraft, ...]:
        ids = self.connection.execute(
            "SELECT draft_id FROM document_drafts WHERE repo = ? ORDER BY created_at, rowid",
            (repo,),
        )
        return tuple(self.get(row[0]) for row in ids)

    def for_plan(self, repo: str, plan_id: str) -> DocumentDraft | None:
        row = self.connection.execute(
            "SELECT draft_id FROM document_drafts WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        return self.get(row[0]) if row else None

    def unplanned_approved(
        self, repo: str, repository: Path, base_branch: str,
    ) -> tuple[DocumentDraft, ...]:
        """Return fresh approvals which do not yet own a published plan."""
        result = []
        ids = tuple(row[0] for row in self.connection.execute(
            "SELECT draft_id FROM document_drafts WHERE repo = ? "
            "AND state = 'approved' AND plan_id IS NULL ORDER BY created_at, rowid",
            (repo,),
        ))
        for draft_id in ids:
            try:
                draft = self.get(draft_id)
                self.assert_current(draft, repository, base_branch)
            except DocumentDraftError as error:
                self.record_publication_error(str(draft_id), str(error))
            else:
                result.append(draft)
        return tuple(result)

    def planning_candidate(
        self, repo: str, repository: Path, base_branch: str,
    ) -> DocumentDraft | None:
        drafts = self.unplanned_approved(repo, repository, base_branch)
        # A plan for another draft cites R-027. Publish an approved edit to
        # R-027 first so that new plan does not stale its source snapshot.
        return min(drafts, key=lambda draft: draft.target_path != R027_SOURCE) if drafts else None

    def bind_plan(self, repo: str, draft_id: str, plan_id: str) -> None:
        draft = self.get(draft_id)
        if draft.repo != repo or draft.state != "approved":
            raise DocumentDraftError("게시할 문서 수정본 승인이 유효하지 않습니다.")
        if draft.plan_id == plan_id:
            return
        if draft.plan_id is not None:
            raise DocumentDraftError("문서 수정본에 이미 다른 ExecPlan이 있습니다.")
        try:
            changed = self.connection.execute(
                "UPDATE document_drafts SET plan_id = ? WHERE draft_id = ? "
                "AND repo = ? AND plan_id IS NULL AND state = 'approved'",
                (plan_id, draft_id, repo),
            )
        except sqlite3.IntegrityError as error:
            raise DocumentDraftError(
                "ExecPlan ID가 다른 문서 수정본에 이미 연결됐습니다."
            ) from error
        if changed.rowcount != 1:
            raise DocumentDraftError("문서 수정본과 ExecPlan의 연결이 변경됐습니다.")

    def record_publication_error(self, draft_id: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE document_drafts SET publication_error = ? WHERE draft_id = ?",
            (reason, draft_id),
        )

    def assert_current(
        self, draft: DocumentDraft, repository: Path, base_branch: str,
    ) -> str:
        """Check the latest remote original and nonterminal source graph."""
        revision = remote_revision(repository, base_branch)
        original_revision, content = document_at(
            repository, revision, draft.target_path,
        )
        relationships = checked_source_relationships(
            self.connection, draft.repo, repository, revision, draft.target_path,
            exclude_plan_id=draft.plan_id,
        )
        if (draft.state != "approved"
            or original_revision != draft.original_revision
            or _hash(content) != draft.original_hash
            or relationships != draft.source_relationships):
            raise DocumentDraftError(
                "승인 뒤 원본 revision·내용 또는 비종료 ExecPlan source 관계가 변경됐습니다."
            )
        return revision

    def generation_prompt(
        self, repo: str, repository: Path, base_branch: str, path: str,
    ) -> tuple[str, tuple[str, str, str]]:
        """Prepare a pinned, read-only Agent request for one assessed document."""
        ownership_clear(self.connection, repo)
        report = self._assessment(repo)
        findings = [finding for finding in report["findings"]
                    if finding["kind"] == "document"
                    and finding["status"] == "candidate"
                    and finding["path"] == path]
        if not findings or not _document_path(path):
            raise DocumentDraftError("의미 보존 문서 후보에 없는 경로입니다.")
        revision = remote_revision(repository, base_branch)
        document_revision, content = document_at(repository, revision, path)
        relationships = checked_source_relationships(
            self.connection, repo, repository, revision, path,
        )
        identity = (document_revision, _hash(content), relationships)
        prior = self.connection.execute(
            "SELECT 1 FROM document_drafts WHERE repo = ? AND target_path = ? "
            "AND original_revision = ? AND original_hash = ? "
            "AND source_relationships = ? AND state != 'stale' LIMIT 1",
            (repo, path, *identity),
        ).fetchone()
        if prior is not None:
            raise DocumentDraftError("같은 원본과 source 관계에 이미 검토할 수정본이 있습니다.")
        prompt = (
            "당신은 Loop Manager 자체의 일회성 제품 문서 수정본 작성자다. "
            "원본 checkout 파일을 수정하지 마라. 아래 전수 평가 candidate 발견의 표현·구조·중복만 "
            "개선하고, 제품 동작·정책·Acceptance Criteria·결정·예외·실패 흐름을 정확히 보존하라. "
            "의미 보존을 확신할 수 없거나 정책 선택이 필요하면 수정본을 만들지 마라. "
            "최종 응답에 DOCUMENT_DRAFT_RESULT: 접두사의 JSON 한 줄을 출력하라. "
            '가능한 형식은 {"status":"candidate","content":"<문서 전체 UTF-8 텍스트>",'
            '"meaning_evidence":"<구체적 보존 근거>"} 또는 '
            '{"status":"uncertain","reason":"<구체적 이유>"}다. '
            "content에는 문서 전체를 빠짐없이 넣어라.\n"
            f"대상: {path}\n문서 revision: {document_revision}\n"
            f"원본 SHA256: {_hash(content)}\n"
            "평가 발견: " + json.dumps(findings, ensure_ascii=False) + "\n"
            "원본 전체:\n" + content.decode("utf-8")
        )
        return prompt, identity

    def create(
        self, repo: str, repository: Path, base_branch: str, path: str,
        proposed_content: bytes, meaning_evidence: str,
        *, expected_identity: tuple[str, str, str] | None = None,
    ) -> DocumentDraft:
        ownership_clear(self.connection, repo)
        report = self._assessment(repo)
        finding_ids = self.candidates(repo).get(path)
        if not finding_ids:
            raise DocumentDraftError("의미 보존 문서 후보에 없는 경로입니다.")
        if len(meaning_evidence.strip()) < 20:
            raise DocumentDraftError("의미 보존 근거를 구체적으로 입력해야 합니다.")
        if not proposed_content or len(proposed_content) > 2_000_000:
            raise DocumentDraftError("수정본은 비어 있거나 2 MB를 넘을 수 없습니다.")
        revision = remote_revision(repository, base_branch)
        original_revision, original_content = document_at(repository, revision, path)
        assessment_revision = report["source_revision"]
        _, assessed_content = document_at(repository, assessment_revision, path)
        for finding in report["findings"]:
            if finding["id"] in finding_ids and finding["source_hash"] != _hash(assessed_content):
                raise DocumentDraftError("전수 평가 문서 지문이 일치하지 않습니다.")
        try:
            original_content.decode("utf-8")
            proposed_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentDraftError("문서 수정본은 UTF-8이어야 합니다.") from error
        if original_content == proposed_content:
            raise DocumentDraftError("원본과 동일한 수정본은 등록할 수 없습니다.")
        diff = _complete_diff(path, original_content, proposed_content)
        relationships = checked_source_relationships(
            self.connection, repo, repository, revision, path,
        )
        if expected_identity is not None and expected_identity != (
            original_revision, _hash(original_content), relationships,
        ):
            raise DocumentDraftError("수정본 생성 중 원본 또는 source 관계가 변경됐습니다.")
        existing = self.connection.execute(
            "SELECT draft_id FROM document_drafts WHERE repo = ? AND target_path = ? "
            "AND original_revision = ? AND original_hash = ? "
            "AND source_relationships = ? AND state != 'stale' LIMIT 1",
            (repo, path, original_revision, _hash(original_content), relationships),
        ).fetchone()
        if existing is not None:
            raise DocumentDraftError("같은 원본과 source 관계에 이미 검토할 수정본이 있습니다.")
        draft_id = uuid4().hex
        self.connection.execute(
            "INSERT INTO document_drafts (draft_id, repo, finding_ids, target_path, "
            "assessment_revision, original_revision, original_hash, original_content, "
            "proposed_hash, proposed_content, complete_diff, source_relationships, "
            "meaning_evidence, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (draft_id, repo, json.dumps(finding_ids), path, assessment_revision,
             original_revision, _hash(original_content), original_content,
             _hash(proposed_content), proposed_content, diff, relationships,
             meaning_evidence),
        )
        return self.get(draft_id)

    def decide(
        self, draft_id: str, proposed_hash: str, decision: str,
        repository: Path, base_branch: str, *, uncertain_reason: str = "",
    ) -> DocumentDraft:
        draft = self.get(draft_id)
        if draft.proposed_hash != proposed_hash or decision not in {"approve", "reject"}:
            raise DocumentDraftError("결정 대상 수정본 ID·해시 또는 결정값이 유효하지 않습니다.")
        if draft.state != "pending":
            raise DocumentDraftError("이미 결정되거나 새 검토가 필요한 수정본입니다.")
        ownership_clear(self.connection, draft.repo)
        reason = None
        if decision == "approve":
            if uncertain_reason:
                decision = "stale"
                reason = "의미 보존 불확실: " + uncertain_reason
            else:
                revision = remote_revision(repository, base_branch)
                try:
                    current_revision, current_content = document_at(
                        repository, revision, draft.target_path,
                    )
                    relationships = checked_source_relationships(
                        self.connection, draft.repo, repository, revision,
                        draft.target_path,
                    )
                except DocumentDraftError:
                    current_revision, current_content, relationships = "", b"", ""
                if (current_revision != draft.original_revision
                    or _hash(current_content) != draft.original_hash
                    or relationships != draft.source_relationships):
                    decision = "stale"
                    reason = "원본 revision·내용 또는 비종료 ExecPlan source 관계가 변경됐습니다."
        elif uncertain_reason:
            raise DocumentDraftError("의미 보존 불확실 사유는 승인 요청에만 사용합니다.")
        token = uuid4().hex
        self.connection.execute(
            "UPDATE document_drafts SET state = ?, requested_decision = ?, decision_revision = ?, "
            "decision_reason = ?, decided_at = CURRENT_TIMESTAMP "
            "WHERE draft_id = ? AND state = 'pending'",
            ("approved" if decision == "approve" else "rejected" if decision == "reject" else "stale",
             "approve" if decision in {"approve", "stale"} else "reject",
             token, reason, draft_id),
        )
        return self.get(draft_id)

    def approved(self, repo: str, repository: Path, base_branch: str) -> tuple[DocumentDraft, ...]:
        """Expose only approvals whose source identity still matches the remote."""
        revision = remote_revision(repository, base_branch)
        result = []
        for draft in self.list(repo):
            if draft.state != "approved":
                continue
            try:
                current_revision, content = document_at(repository, revision, draft.target_path)
                relationships = checked_source_relationships(
                    self.connection, repo, repository, revision, draft.target_path,
                    exclude_plan_id=draft.plan_id,
                )
            except DocumentDraftError:
                continue
            if (current_revision == draft.original_revision
                and _hash(content) == draft.original_hash
                and relationships == draft.source_relationships):
                result.append(draft)
        return tuple(result)


def review_artifact_bytes(draft: DocumentDraft) -> bytes:
    """Render length-delimited review data without changing document line endings."""
    header = (
        f"Draft ID: {draft.draft_id}\nState: {draft.state}\n"
        f"Target: {draft.target_path}\nFindings: {', '.join(draft.finding_ids)}\n"
        f"Assessment revision: {draft.assessment_revision}\n"
        f"Original revision: {draft.original_revision}\nOriginal SHA256: {draft.original_hash}\n"
        f"Proposed SHA256: {draft.proposed_hash}\n"
        f"Nonterminal sources: {draft.source_relationships}\n"
        f"Meaning evidence: {draft.meaning_evidence}\n"
        f"Requested decision: {draft.requested_decision or ''}\n"
        f"Decision revision: {draft.decision_revision or ''}\n"
        f"Decision reason: {draft.decision_reason or ''}\n"
        f"Publication check: {draft.publication_error or ''}\n"
        f"ExecPlan ID: {draft.plan_id or ''}\n"
    )
    diff = draft.complete_diff.encode("utf-8")
    return (
        header.encode("utf-8")
        + f"\n=== Complete original ({len(draft.original_content)} bytes) ===\n".encode("ascii")
        + draft.original_content
        + f"\n=== Complete proposal ({len(draft.proposed_content)} bytes) ===\n".encode("ascii")
        + draft.proposed_content
        + f"\n=== Complete diff ({len(diff)} bytes) ===\n".encode("ascii")
        + diff
    )


def review_artifact(draft: DocumentDraft) -> str:
    """Return the same review data as text for callers that need a string."""
    return review_artifact_bytes(draft).decode("utf-8")


def parse_generated_draft(output: str) -> tuple[bytes, str] | None:
    prefix = "DOCUMENT_DRAFT_RESULT: "
    lines = [line[len(prefix):] for line in output.splitlines()
             if line.startswith(prefix)]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > 2_100_000:
        raise DocumentDraftError("수정본 Agent 결과 JSON 한 줄이 필요합니다.")
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise DocumentDraftError("수정본 Agent 결과가 JSON이 아닙니다.") from error
    if not isinstance(result, dict):
        raise DocumentDraftError("수정본 Agent 결과 형식이 유효하지 않습니다.")
    if result.get("status") == "uncertain" and set(result) == {"status", "reason"}:
        if not isinstance(result["reason"], str) or not result["reason"].strip():
            raise DocumentDraftError("의미 보존 불확실 사유가 없습니다.")
        return None
    if result.get("status") != "candidate" or set(result) != {
        "status", "content", "meaning_evidence",
    } or not isinstance(result["content"], str) or not isinstance(result["meaning_evidence"], str):
        raise DocumentDraftError("수정본 Agent 결과 형식이 유효하지 않습니다.")
    return result["content"].encode("utf-8"), result["meaning_evidence"]
