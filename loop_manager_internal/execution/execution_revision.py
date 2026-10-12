"""Pure revision classification and phase invalidation for ExecPlan execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Iterable


class DefinitionDelta(str, Enum):
    """Declaration fields whose execution meaning can change independently."""

    PATH = "path"
    TITLE = "title"
    SOURCES = "sources"
    DEPENDENCIES = "depends_on"
    PROFILE = "profile"
    PRIORITY = "priority"
    BODY = "body"
    SOURCE_INPUT = "source_input"
    ARCHIVE_SCOPE = "archive_scope"
    GUIDANCE = "guidance"


class RevisionGateKind(str, Enum):
    """Typed result consumed by the execution handler before phase effects."""

    CONTINUE = "continue"
    WAIT = "wait"
    REFRESH_WORKTREE = "refresh-worktree"
    INVALIDATE_TO_PHASE = "invalidate-to-phase"
    FINAL_SAFETY_ERROR = "final-safety-error"


_PHASE_ORDER = {
    "IMPLEMENTING": 0,
    "VERIFYING": 1,
    "REVIEWING": 2,
    "PUBLISHING": 3,
}


_REMOTE_FAILURE_REASONS = frozenset({
    "verify_retry_exhausted",
    "review_retry_exhausted",
})


def remote_failure_correction_paths(
    reason_code: str,
    changed_paths: Iterable[str],
    failure_evidence: str,
) -> tuple[str, ...]:
    """Select unverified inputs for Worker reassessment of exhausted validation.

    A diagnostic path is not a dependency graph: an implementation, fixture or
    build input absent from both the traceback and checkpoint can fix the cause.
    Only clearly documentary changes need an explicit diagnostic connection.
    Scope, ownership and reason-specific approval gates are enforced by callers.
    """
    if reason_code not in _REMOTE_FAILURE_REASONS:
        return ()
    evidence = failure_evidence.replace("\\", "/")
    candidates = []
    for path in changed_paths:
        file = PurePosixPath(path)
        documentary = (
            path.startswith("docs/")
            or file.suffix.lower() in {".md", ".rst"}
            or file.name.lower() in {"readme", "license", "copying", "changelog"}
        )
        if not documentary or re.search(
            rf"(?<![\w.-]){re.escape(path)}(?![\w./-])", evidence,
        ):
            candidates.append(path)
    return tuple(candidates)


def _digest(label: str, values: Iterable[object]) -> str:
    digest = hashlib.sha256(f"loop-manager-{label}-v1\0".encode("ascii"))
    for value in values:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class DefinitionSnapshot:
    """The reconciled plan and source inputs used by an execution stage."""

    revision_sha: str
    path: str
    title: str
    sources: tuple[str, ...]
    depends_on: tuple[str, ...]
    complexity: str
    risk: str
    priority: str
    body_hash: str
    source_fingerprint: str
    archive_candidates: tuple[str, ...]
    guidance_hash: str = ""
    worker_guidance_hash: str = ""
    body_text: str | None = None

    def serialize(self) -> str:
        return json.dumps(
            {
                "revision_sha": self.revision_sha,
                "path": self.path,
                "title": self.title,
                "sources": list(self.sources),
                "depends_on": list(self.depends_on),
                "complexity": self.complexity,
                "risk": self.risk,
                "priority": self.priority,
                "body_hash": self.body_hash,
                "source_fingerprint": self.source_fingerprint,
                "archive_candidates": list(self.archive_candidates),
                "guidance_hash": self.guidance_hash,
                "worker_guidance_hash": self.worker_guidance_hash,
                "body_text": self.body_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def deserialize(cls, value: str | None) -> "DefinitionSnapshot | None":
        if not value:
            return None
        try:
            payload = json.loads(value)
            return cls(
                revision_sha=str(payload["revision_sha"]),
                path=str(payload["path"]),
                title=str(payload["title"]),
                sources=tuple(str(item) for item in payload["sources"]),
                depends_on=tuple(
                    str(item) for item in payload["depends_on"]
                ),
                complexity=str(payload["complexity"]),
                risk=str(payload["risk"]),
                priority=str(payload["priority"]),
                body_hash=str(payload["body_hash"]),
                source_fingerprint=str(payload["source_fingerprint"]),
                archive_candidates=tuple(
                    str(item) for item in payload["archive_candidates"]
                ),
                guidance_hash=str(payload.get("guidance_hash", "")),
                worker_guidance_hash=str(payload.get("worker_guidance_hash", "")),
                body_text=payload.get("body_text"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("저장된 execution definition snapshot이 유효하지 않습니다.") from error

    @property
    def definition_token(self) -> str:
        """Token for the complete current declaration used by safety checks."""

        return _digest(
            "execution-definition",
            (
                self.path,
                self.title,
                self.sources,
                self.depends_on,
                self.complexity,
                self.risk,
                self.priority,
                self.body_hash,
                self.source_fingerprint,
                self.archive_candidates,
                self.guidance_hash,
            ),
        )

    @property
    def worker_token(self) -> str:
        """Token for fields that require a fresh Worker result when changed."""
        values = (
            self.sources,
            self.depends_on,
            self.worker_body_hash,
            self.source_fingerprint,
            self.archive_candidates,
            self.guidance_hash,
        )
        # Keep legacy tokens byte-for-byte until actual delivery is recorded.
        if self.worker_guidance_hash:
            values += (self.worker_guidance_hash,)
        return _digest('worker-input', values)

    @property
    def worker_body_hash(self) -> str:
        if isinstance(self.body_text, str) and execplan_body_hash(self.body_text) == self.body_hash:
            return execplan_worker_body_hash(self.body_text)
        return self.body_hash

    @property
    def archive_token(self) -> str:
        return _digest(
            "archive-scope",
            (self.sources, self.archive_candidates),
        )

    def worker_candidate_token(self, head: str) -> str:
        return _digest('worker-candidate', (self.worker_token, head))

    def verify_token(
        self, base_token: str, candidate_head: str | None = None,
        verify_output: str | None = None,
    ) -> str:
        values = (self.worker_token, base_token, candidate_head)
        if self.body_text is not None:
            values += (self.body_hash,)
        if verify_output is not None:
            # A successful verify marker must describe the output stored beside
            # it, so a later failed attempt cannot inherit stale success.
            return _digest("verify-stage-output", (*values, verify_output))
        return _digest("verify-stage", values)

    def review_token(self, base_token: str, candidate_head: str | None = None) -> str:
        return _digest("review-stage", (self.verify_token(base_token, candidate_head),))

    def publication_token(self, base_token: str, candidate_head: str | None = None) -> str:
        return _digest(
            "publication-stage",
            (self.definition_token, self.review_token(base_token, candidate_head)),
        )


def execplan_body_hash(text: str) -> str:
    """Hash only the Markdown body so metadata-only edits stay distinguishable."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("ExecPlan frontmatter가 없습니다.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("ExecPlan frontmatter 종료 구분자가 없습니다.") from error
    body = "\n".join(lines[end + 1 :]).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


class ExecPlanBodyHash(str):
    """Retain the observed original alongside its compatible full-body digest."""
    def __new__(cls, text: str):
        value = super().__new__(cls, execplan_body_hash(text))
        value.body_text = text
        return value


def execplan_worker_body_hash(text: str) -> str:
    """Normalize only check marks and unambiguous, atomic past test records.

    Free-form prose stays an input even under Progress or Decision Log. This
    deliberately small grammar does not infer intent from section names.
    """
    section = None
    lines = []
    # ponytail: Free-form past records cause conservative reassessment; extend
    # only the explicit fact grammar with regression cases, never omit sections.
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line.startswith('#'):
            section = line if line in {'## Progress', '## Decision Log'} else None
        if section == '## Progress':
            line = re.sub(r'^(\s*- \[)[ xX](\] )', r'\1 \2', line)
        if section and re.fullmatch(
            r'- \d{4}-\d{2}-\d{2} 실행 기록: `[^`\n]+` → (?:PASS|FAIL) \(\d+ tests\)\.', line,
        ):
            continue
        lines.append(line)
    return execplan_body_hash('\n'.join(lines))


def classify_definition_delta(
    previous: DefinitionSnapshot,
    current: DefinitionSnapshot,
) -> frozenset[DefinitionDelta]:
    """Classify declaration changes without treating a new Git SHA as meaning."""

    changed: set[DefinitionDelta] = set()
    if previous.path != current.path:
        changed.add(DefinitionDelta.PATH)
    if previous.title != current.title:
        changed.add(DefinitionDelta.TITLE)
    if previous.sources != current.sources:
        changed.add(DefinitionDelta.SOURCES)
    if previous.depends_on != current.depends_on:
        changed.add(DefinitionDelta.DEPENDENCIES)
    if (previous.complexity, previous.risk) != (
        current.complexity,
        current.risk,
    ):
        changed.add(DefinitionDelta.PROFILE)
    if previous.priority != current.priority:
        changed.add(DefinitionDelta.PRIORITY)
    # One-sided normalization could accidentally match a legacy full hash.
    # Both original documents are required for a record-only comparison.
    previous_body, current_body = previous.body_hash, current.body_hash
    if previous.body_text is not None and current.body_text is not None:
        previous_body, current_body = previous.worker_body_hash, current.worker_body_hash
    if previous_body != current_body:
        changed.add(DefinitionDelta.BODY)
    if previous.source_fingerprint != current.source_fingerprint:
        changed.add(DefinitionDelta.SOURCE_INPUT)
    if previous.archive_candidates != current.archive_candidates:
        changed.add(DefinitionDelta.ARCHIVE_SCOPE)
    if previous.guidance_hash != current.guidance_hash:
        changed.add(DefinitionDelta.GUIDANCE)
    return frozenset(changed)


WORKER_INVALIDATING_DELTAS = frozenset(
    {
        DefinitionDelta.SOURCES,
        DefinitionDelta.SOURCE_INPUT,
        DefinitionDelta.DEPENDENCIES,
        DefinitionDelta.BODY,
        DefinitionDelta.ARCHIVE_SCOPE,
        DefinitionDelta.GUIDANCE,
    }
)


@dataclass(frozen=True)
class StageArtifacts:
    document_review_pending: bool = False
    preliminary_review_pending: bool = False
    manual_submission: bool = False
    require_worker_candidate: bool = False
    worker_candidate_token: str | None = None
    worker_result: bool = False
    verify_result: bool = False
    review_result: bool = False
    worker_input_token: str | None = None
    archive_token: str | None = None
    verify_token: str | None = None
    verify_output: str | None = None
    review_token: str | None = None
    publication_token: str | None = None


@dataclass(frozen=True)
class RevisionGateDecision:
    kind: RevisionGateKind
    phase: str
    deltas: frozenset[DefinitionDelta] = frozenset()
    unmet_dependencies: tuple[str, ...] = ()
    detail: str | None = None


def _earlier_phase(current: str, candidate: str) -> str:
    return candidate if _PHASE_ORDER[candidate] < _PHASE_ORDER[current] else current


def decide_revision_gate(
    *,
    phase: str,
    previous: DefinitionSnapshot | None,
    current: DefinitionSnapshot,
    active_base_token: str,
    current_base_token: str,
    artifacts: StageArtifacts,
    unmet_dependencies: tuple[str, ...] = (),
    candidate_head: str | None = None,
) -> RevisionGateDecision:
    """Choose the earliest safe phase while preserving execution identity."""

    if phase not in _PHASE_ORDER:
        return RevisionGateDecision(
            RevisionGateKind.FINAL_SAFETY_ERROR,
            phase,
            detail=f"revision gate가 지원하지 않는 실행 phase입니다: {phase}",
        )

    deltas = (
        classify_definition_delta(previous, current)
        if previous is not None
        else frozenset()
    )
    target = phase
    if previous is None and phase != "IMPLEMENTING" and not artifacts.manual_submission:
        # A pre-G-037 row may contain successful artifacts without evidence of
        # the definition that produced them. Conservatively resume its Worker.
        target = "IMPLEMENTING"
    elif deltas & WORKER_INVALIDATING_DELTAS:
        target = "VERIFYING" if artifacts.manual_submission and phase != 'IMPLEMENTING' else "IMPLEMENTING"

    if phase != "IMPLEMENTING" and not artifacts.manual_submission and (
        not artifacts.worker_result
        or artifacts.worker_input_token != current.worker_token
        or artifacts.archive_token != current.archive_token
        or (artifacts.require_worker_candidate and (
            not candidate_head
            or artifacts.worker_candidate_token != current.worker_candidate_token(candidate_head)))
    ):
        target = "IMPLEMENTING"

    base_changed = active_base_token != current_base_token
    if base_changed and target != "IMPLEMENTING":
        target = _earlier_phase(target, "VERIFYING")

    expected_verify = current.verify_token(
        current_base_token, candidate_head, artifacts.verify_output,
    )
    if target in {"REVIEWING", "PUBLISHING"} and not (target == "REVIEWING" and (
            artifacts.document_review_pending or artifacts.preliminary_review_pending)) and (
        not artifacts.verify_result or artifacts.verify_token != expected_verify
    ):
        target = _earlier_phase(target, "VERIFYING")
    if target == "PUBLISHING" and (
        not artifacts.review_result
        or artifacts.review_token != current.review_token(current_base_token, candidate_head)
    ):
        target = "REVIEWING"

    if unmet_dependencies:
        return RevisionGateDecision(
            RevisionGateKind.WAIT,
            target,
            deltas,
            tuple(sorted(set(unmet_dependencies))),
            detail="미충족 dependency Merge 대기: "
            + ", ".join(sorted(set(unmet_dependencies))),
        )
    if target != phase:
        return RevisionGateDecision(
            RevisionGateKind.INVALIDATE_TO_PHASE,
            target,
            deltas,
        )
    if base_changed:
        return RevisionGateDecision(
            RevisionGateKind.REFRESH_WORKTREE,
            target,
            deltas,
        )
    return RevisionGateDecision(
        RevisionGateKind.CONTINUE,
        target,
        deltas,
    )
