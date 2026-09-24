"""Pure revision classification and phase invalidation for ExecPlan execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
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
    checkpoint_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    """Select candidates for reassessment, without claiming the cause is fixed.

    Diagnostics often name only a test, or no path at all. Changes to files
    in the blocked implementation also warrant fresh Worker assessment.
    """
    if reason_code not in _REMOTE_FAILURE_REASONS:
        return ()
    evidence = failure_evidence.replace("\\", "/")
    checkpoint_files = frozenset(checkpoint_paths)
    return tuple(
        path for path in changed_paths
        if path in checkpoint_files or re.search(
            rf"(?<![\w.-]){re.escape(path)}(?![\w./-])",
            evidence,
        )
    )


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
            ),
        )

    @property
    def worker_token(self) -> str:
        """Token for fields that require a fresh Worker result when changed."""

        return _digest(
            "worker-input",
            (
                self.path,
                self.sources,
                self.depends_on,
                self.body_hash,
                self.source_fingerprint,
                self.archive_candidates,
            ),
        )

    @property
    def archive_token(self) -> str:
        return _digest(
            "archive-scope",
            (self.sources, self.archive_candidates),
        )

    def verify_token(self, base_token: str) -> str:
        return _digest("verify-stage", (self.worker_token, base_token))

    def review_token(self, base_token: str) -> str:
        return _digest("review-stage", (self.verify_token(base_token),))

    def publication_token(self, base_token: str) -> str:
        return _digest(
            "publication-stage",
            (self.definition_token, self.review_token(base_token)),
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
    if previous.body_hash != current.body_hash:
        changed.add(DefinitionDelta.BODY)
    if previous.source_fingerprint != current.source_fingerprint:
        changed.add(DefinitionDelta.SOURCE_INPUT)
    if previous.archive_candidates != current.archive_candidates:
        changed.add(DefinitionDelta.ARCHIVE_SCOPE)
    return frozenset(changed)


WORKER_INVALIDATING_DELTAS = frozenset(
    {
        DefinitionDelta.PATH,
        DefinitionDelta.SOURCES,
        DefinitionDelta.DEPENDENCIES,
        DefinitionDelta.BODY,
        DefinitionDelta.SOURCE_INPUT,
        DefinitionDelta.ARCHIVE_SCOPE,
    }
)


@dataclass(frozen=True)
class StageArtifacts:
    worker_result: bool = False
    verify_result: bool = False
    review_result: bool = False
    worker_input_token: str | None = None
    archive_token: str | None = None
    verify_token: str | None = None
    review_token: str | None = None
    publication_token: str | None = None


@dataclass(frozen=True)
class RevisionGateDecision:
    kind: RevisionGateKind
    phase: str
    deltas: frozenset[DefinitionDelta] = frozenset()
    unmet_dependencies: tuple[str, ...] = ()
    preserve_validation: bool = False
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
    base_requires_validation: bool = False,
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
    if previous is None and phase != "IMPLEMENTING":
        # A pre-G-037 row may contain successful artifacts without evidence of
        # the definition that produced them. Conservatively resume its Worker.
        target = "IMPLEMENTING"
    elif deltas & WORKER_INVALIDATING_DELTAS:
        target = "IMPLEMENTING"

    if phase != "IMPLEMENTING" and (
        not artifacts.worker_result
        or artifacts.worker_input_token != current.worker_token
        or artifacts.archive_token != current.archive_token
    ):
        target = "IMPLEMENTING"

    base_changed = active_base_token != current_base_token
    if base_changed and base_requires_validation and target != "IMPLEMENTING":
        target = _earlier_phase(target, "VERIFYING")

    expected_verify = current.verify_token(current_base_token)
    if target in {"REVIEWING", "PUBLISHING"} and (
        not artifacts.verify_result or artifacts.verify_token != expected_verify
    ):
        # A harmless declaration-only rebase can deliberately carry successful
        # validation forward; the handler rewrites its token after the rebase.
        if not (base_changed and not base_requires_validation and artifacts.verify_result):
            target = _earlier_phase(target, "VERIFYING")
    if target == "PUBLISHING" and (
        not artifacts.review_result
        or artifacts.review_token != current.review_token(current_base_token)
    ):
        if not (base_changed and not base_requires_validation and artifacts.review_result):
            target = "REVIEWING"

    preserve_validation = bool(
        base_changed
        and not base_requires_validation
        and not (deltas & WORKER_INVALIDATING_DELTAS)
        and target == phase
    )
    if unmet_dependencies:
        return RevisionGateDecision(
            RevisionGateKind.WAIT,
            target,
            deltas,
            tuple(sorted(set(unmet_dependencies))),
            preserve_validation=preserve_validation,
            detail="미충족 dependency Merge 대기: "
            + ", ".join(sorted(set(unmet_dependencies))),
        )
    if target != phase:
        return RevisionGateDecision(
            RevisionGateKind.INVALIDATE_TO_PHASE,
            target,
            deltas,
            preserve_validation=False,
        )
    if base_changed:
        return RevisionGateDecision(
            RevisionGateKind.REFRESH_WORKTREE,
            target,
            deltas,
            preserve_validation=preserve_validation,
        )
    return RevisionGateDecision(
        RevisionGateKind.CONTINUE,
        target,
        deltas,
    )
