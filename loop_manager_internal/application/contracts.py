"""application contracts boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from loop_manager_internal.agents.agent_invocation import AgentResult
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningContext
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
from loop_manager_internal.planning.planning_catalog import PlanDeletion
from loop_manager_internal.recovery import recovery_runtime
from pathlib import Path
import re
import sqlite3


class ProjectRunKind(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    CONFIGURATION_ERROR = "configuration_error"
    OPERATIONAL_ERROR = "operational_error"
    RUNTIME_HANDOFF = "runtime_handoff"


@dataclass(frozen=True)
class ProjectRunResult:
    kind: ProjectRunKind
    exit_code: int
    detail: str | None = None


@dataclass(frozen=True)
class ScheduledProjectResult:
    repo: str
    result: ProjectRunResult


@dataclass(frozen=True)
class CatalogScheduleDecision:
    active: sqlite3.Row | None
    has_ready: bool
    runnable_ready: sqlite3.Row | None
    has_plan_blocked: bool = False
    has_pr_open: bool = False
    project_blocked: bool = False

    @property
    def has_waiting(self) -> bool:
        return self.has_plan_blocked or self.has_pr_open or self.project_blocked


@dataclass(frozen=True)
class ProjectRegistrationInspection:
    root: Path
    base_branch: str
    provider: str
    agents_hash: str
    agents_approved: bool
    origin: str | None = None
    source_revision: str | None = None


@dataclass(frozen=True)
class ValidationFailureDecision:
    stage: str
    fingerprint: str
    occurrences: int
    blocked: bool
    recovery: recovery_runtime.RecoveryDecision | None = None


@dataclass(frozen=True)
class WorkerLevelDecision:
    previous_level: int
    effective_level: int
    review_history: dict[str, dict[str, int]]

    @property
    def escalated(self) -> bool:
        return self.effective_level > self.previous_level


@dataclass(frozen=True)
class CleanupAssessment:
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkerPublication:
    """The Worker-provided metadata used for its commit and pull request."""

    commit_title: str
    summary: str
    test_evidence: str


class RequirementArchiveError(RuntimeError):
    """Requirement archive eligibility or movement cannot be proven safe."""


class RequirementArchiveCollisionError(RequirementArchiveError):
    """A Requirement archive destination is already occupied."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Requirement archive 대상이 이미 존재합니다: {path}")


class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""


class ExecPlanMetadataError(RuntimeError):
    """The selected ExecPlan declaration disagrees with the catalog."""


class ReviewerProtocolError(RuntimeError):
    """A failed Reviewer response does not identify actionable issues."""


class RemoteBranchMissingError(RuntimeError):
    """A successful remote query confirmed that the requested branch is absent."""


@dataclass(frozen=True)
class ValidatedPlanningResult:
    """Validated files to publish, separated from executable plans."""

    plans: tuple[tuple[str, dict[str, object]], ...]
    publication_paths: tuple[str, ...]
    publication_hashes: tuple[tuple[str, str], ...] = ()
    revisions: tuple[PlanRevision, ...] = ()
    deletions: tuple[PlanDeletion, ...] = ()
    mutable_snapshot: MutableReadySnapshot | None = None
    maintenance_context: MaintenancePlanningContext | None = None
    refactoring_plan_ids: tuple[str, ...] = ()
    revalidation_plan_ids: tuple[str, ...] = ()


class VerifyOutput(str):
    """Bounded persisted feedback that retains full output for fingerprinting."""

    fingerprint_source: str
    timed_out: bool
    exit_code: int | None

    def __new__(cls, output: str, *, timed_out: bool = False,
                exit_code: int | None = None) -> VerifyOutput:
        shown = redact_secrets(output) if timed_out else output
        instance = super().__new__(cls, shown.strip()[-8000:])
        instance.fingerprint_source = output
        instance.timed_out = timed_out
        instance.exit_code = exit_code
        return instance


@dataclass(frozen=True)
class PullRequestSyncResult:
    step_result: int | None = None
    conflict_started: bool = False
    merged: bool = False


@dataclass(frozen=True)
class WaitingPullRequestObservation:
    conflicts: tuple[tuple[sqlite3.Row, Mapping[str, object]], ...]
    merged: bool
    merged_pr_urls: tuple[str, ...] = ()


MAX_VALIDATION_FINGERPRINT_REPEATS = 3


MAX_REVIEWER_PROTOCOL_ATTEMPTS = 3


PROJECT_STEP_TRANSIENT_PAUSE = 3


PROJECT_STEP_USAGE_PAUSE = 4


TRANSIENT_AGENT_ERROR_PREFIX = "일시적 Agent 실패 (상태 보존):"


USAGE_LIMIT_ERROR_PREFIX = "AI 사용 한도 소진 (상태 보존):"


PLAN_SHA_BACKFILL_ERROR = "계획 commit SHA를 기존 작업에서 안전하게 복구할 수 없습니다."


PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}


CodexResult = AgentResult


_FUTURE_IDEA_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


_TIMESTAMP_NOISE = re.compile(
    r"(?ix)"
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)


_DURATION_NOISE = re.compile(
    r"(?i)\b(?:in|after|elapsed[:=]?)\s+\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?)\b"
)


_TEMP_PATH_NOISE = re.compile(
    r"(?i)(?<![\w])"
    r"(?P<root>[a-z]:[\\/][^\r\n]*?[\\/]temp|/(?:private/)?(?:var/)?tmp)"
    r"(?P<tail>[\\/][^\s:]+)"
)


_TEMP_DIRECTORY_CONTEXT = re.compile(
    r"(?i)(?:\b(?:temporary\s+directory|directory|workspace|worktree)\b"
    r"|디렉터리|작업\s*공간)"
)


_REVIEW_ISSUE_ID = re.compile(
    r"(?mi)^[ \t]*ISSUE_ID:[ \t]*(?P<identifier>[^\r\n]+?)[ \t]*$"
)


_REVIEW_ISSUE_DECLARATION = re.compile(r"(?i)^[ \t]*ISSUE_ID\b")


_REVIEW_ISSUE_SYMBOL = re.compile(r"(?i)^[a-z0-9_.<>/-]+$")


_REVIEW_ISSUE_CATEGORY = re.compile(r"(?i)^[a-z0-9][a-z0-9-]*$")


_REVIEW_VERDICT_DECLARATION = re.compile(r"(?i)^[ \t]*VERDICT\b")


_REVIEW_VERDICT = re.compile(r"(?i)^[ \t]*VERDICT:[ \t]*(PASS|FAIL|SPEC_BLOCKED)[ \t]*$")


_TARGETED_TEST_FAILURE = re.compile(
    r"(?i)(?<![\w])(?:FAIL(?:ED|URES?)?|ERRORS?|실패"
    r"|(?:did|does|do|has|have|had|was|were|is|are)\s+not\s+pass(?:ed)?"
    r"|(?:not|never|didn't|doesn't|don't)\s+pass(?:ed)?)(?![\w])"
)


_TARGETED_TEST_EXCEPTION_FAILURE = re.compile(
    r"(?m)(?<![\w])[A-Z][A-Za-z0-9_]*(?:Error|Exception)"
    r"(?=[ \t]*(?::|발생|occurred\b|(?:was[ \t]+)?raised\b|$))"
)


_TARGETED_TEST_SUCCESS = re.compile(
    r"(?i)(?<![\w])(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])"
)


_TARGETED_TEST_COMMAND = re.compile(
    r"(?mi)^[ \t]*(?:[-*][ \t]+)?"
    r"(?P<label>FAILED_COMMAND|RERUN):[ \t]*(?P<command>\S.*?)?[ \t]*$"
)


_NO_RUNNABLE_TARGETED_TEST = re.compile(
    r"(?:"
    r"(?:(?:실행 가능한|적용할|관련)\s+)*(?:targeted\s+)?unit\s+tests?(?:가|를|는)?\s+"
    r"(?:없(?:어|어서|었습니다|습니다|다)"
    r"|(?:실행|수행)(?:할\s+수\s+없|하지\s+(?:않았|못했))(?:습니다|다))"
    r"|no runnable targeted (?:unit )?tests?"
    r")",
    re.IGNORECASE,
)


_TARGETED_TEST_METADATA = re.compile(
    r"(?i)(?<![\w])(?:[\w./-]+\s*:\s*)?"
    r"(?:\d+\s+tests?\s+)?"
    r"(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])(?:\s*\([^)]*\))?"
    r"|(?i:(?<![\w])(?:FAILED_COMMAND|RERUN):)"
    r"|(?i:(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b)"
)


_INLINE_TEST_COMMAND = re.compile(
    r"(?i)(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b"
)
