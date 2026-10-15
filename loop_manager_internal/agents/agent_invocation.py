"""Provider-neutral, non-secret observations for physical Agent CLI attempts."""

from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
import re
import subprocess
from uuid import uuid4
from typing import Callable, TYPE_CHECKING

from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence, CodexLaunch

if TYPE_CHECKING:
    from loop_manager_internal.agents.common_guidance import CommonGuidance


@dataclass(frozen=True)
class ResolvedProfile:
    model: str
    effort: str


AgentProfile = ResolvedProfile


class AgentResultKind(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    USAGE_LIMIT = "usage_limit"
    PERMANENT_CONFIGURATION = "permanent_configuration"


def approval_policy_failure(message: str) -> bool:
    """Policy failures cannot become transport retries or session fallback."""
    policy = (r"auto[_ -]?review|automatic (?:approval )?review|approval (?:review|policy)|"
              r"approvals_reviewer|guardian|classifier|sandbox|request_permissions|"
              r"permission[_ -](?:policy|mode)|"
              r"자동 (?:승인 )?검토")
    failure = (r"fail(?:ed|ure)?|error|unavailable|unsupported|disabled|denied|"
               r"reject(?:ed|ion)?|conflict|invalid|cannot|could not|unable|"
               r"not (?:available|supported|allowed|found|enabled)|does not exist|expired|"
               r"timeout|timed? out|실패|거부|중단")
    # Keep line boundaries: a normal sandbox banner followed by an unrelated
    # network/quota error must retain the existing provider recovery behavior.
    return bool(re.search(
        rf"(?:{policy})[^\n]{{0,80}}(?:{failure})|"
        rf"(?:{failure})[^\n]{{0,80}}(?:{policy})", message, re.I))


def classify_agent_result(code: int, message: str) -> AgentResultKind:
    if code == 0:
        return AgentResultKind.SUCCESS
    if approval_policy_failure(message):
        return AgentResultKind.PERMANENT_CONFIGURATION
    normalized = " ".join(message.casefold().split())
    if any(phrase in normalized for phrase in (
        "usage limit", "usage cap", "quota exceeded", "insufficient_quota",
        "credit balance", "usage limit reached", "hit your limit",
        "out of extra usage",
    )):
        return AgentResultKind.USAGE_LIMIT
    if any(phrase in normalized for phrase in (
        "timed out", "timeout", "overloaded", "rate limit exceeded",
        "temporarily unavailable", "service unavailable", "connection reset",
        "connection refused", "network error", "dns error", "gateway timeout",
        "internal server error", "try again later", "http 429", "http 500",
        "http 502", "http 503", "http 504", "too many requests",
    )):
        return AgentResultKind.TRANSIENT_FAILURE
    return AgentResultKind.PERMANENT_CONFIGURATION


def timeout_message(message: str) -> bool:
    """Recognize the timeout spellings used by providers and process wrappers."""
    return bool(re.search(r"\btimeout\b|\btimed out\b", message, re.I))


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    status: str = "unknown"

    def __post_init__(self) -> None:
        if self.status == "unknown" and any(value is not None for value in (
            self.input_tokens, self.cached_input_tokens,
            self.cache_write_input_tokens, self.output_tokens,
            self.reasoning_output_tokens,
        )):
            object.__setattr__(self, "status", "known")


@dataclass(frozen=True)
class AgentResult:
    code: int
    message: str
    session_id: str | None = None
    kind: AgentResultKind | None = None
    usage: Usage = Usage()
    collection_status: str = "unknown"
    isolation_invocation_id: str | None = None

    def __iter__(self):
        return iter((self.code, self.message))

    @property
    def result_kind(self) -> AgentResultKind:
        return self.kind or classify_agent_result(self.code, self.message)

    @property
    def ledger_kind(self) -> str:
        if (self.result_kind is AgentResultKind.TRANSIENT_FAILURE
                and timeout_message(self.message)):
            return "timeout"
        return self.result_kind.value


@dataclass(frozen=True)
class AgentRequest:
    worktree: Path
    state_dir: Path
    config_path: Path
    role_name: str
    prompt: str
    read_only: bool
    level: int | None = None
    persist_session: bool = False
    session_id: str | None = None
    on_session_started: Callable[[str], None] | None = None
    profile: ResolvedProfile | None = None
    on_isolation_checked: Callable[[IsolationEvidence], None] | None = None
    isolation_snapshot_digest: Callable[[], str] | None = None
    expected_repository: str | None = None
    common_guidance: CommonGuidance | None = None
    on_process_started: Callable[[], None] | None = None
    on_process_finished: Callable[[], None] | None = None
    stream_output: bool = False
    prepared_launch: CodexLaunch | None = field(default=None, repr=False)
    on_execution_identity: Callable[[dict[str, str]], None] | None = None
    on_launch_prepared: Callable[[CodexLaunch], None] | None = None


@dataclass(frozen=True)
class InvocationContext:
    repo: str
    plan_id: str | None
    definition_revision: str | None = None
    transition: str | None = None
    transition_cause: str | None = None
    attempt: int = 1
    transition_id: str | None = None
    previous_provider: str | None = None
    previous_model: str | None = None
    previous_effort: str | None = None
    previous_session_id: str | None = None
    task_id: str | None = None
    notification_id: str | None = None
    purpose: str | None = None
    target: str | None = None


class AttemptSequence:
    """Assign physical attempt order without sharing mutable process state."""

    def __init__(self, repo: str, plan_id: str | None, revision: str | None = None,
                 initial_transition: str | None = None,
                 initial_cause: str | None = None, task_id: str | None = None,
                 purpose: str | None = None, target: str | None = None):
        self.repo = repo
        self.plan_id = plan_id
        self.revision = revision
        self.initial_transition = initial_transition
        self.initial_cause = initial_cause
        self.task_id = task_id
        self.purpose = purpose
        self.target = target
        self.notification_id = uuid4().hex
        self.count = 0
        self.previous_failure: str | None = None
        self.fallback_recorded = False

    def next(self, session_id: str | None, fallback: bool) -> InvocationContext:
        self.count += 1
        first_fallback = fallback and not self.fallback_recorded
        if first_fallback:
            self.fallback_recorded = True
            transition, cause = "fallback", "session_unavailable"
        elif self.count == 1:
            transition = self.initial_transition or ("resume" if session_id else "start")
            cause = self.initial_cause
        else:
            transition = "resume" if session_id else "start"
            cause = self.previous_failure
        return InvocationContext(
            self.repo, self.plan_id, self.revision, transition, cause, self.count,
            task_id=self.task_id, notification_id=self.notification_id,
            purpose=self.purpose, target=self.target,
        )


class TrackedAgentCall:
    """Callable that supplies one context for each retry runner attempt."""

    def __init__(self, invoke: Callable[[str | None, bool, InvocationContext], object],
                 attempts: AttemptSequence):
        self.invoke = invoke
        self.attempts = attempts

    def __call__(self, session_id: str | None, fallback: bool):
        return self.invoke(session_id, fallback, self.attempts.next(session_id, fallback))


MAX_TRANSIENT_AGENT_RETRIES = 3

def session_resume_unavailable(message: str) -> bool:
    if approval_policy_failure(message):
        return False
    # shortcut: CLI 오류 문구에 의존하며, 세션 부재 전용 오류가 전달되면 구조화된 판별로 바꾼다.
    normalized = " ".join(message.casefold().split())
    return bool(
        re.search(
            r"(?:session|thread|rollout|conversation).{0,80}(?:not found|does not exist|expired)|"
            r"\bno (?:session|thread|rollout|conversation) found\b",
            normalized,
        )
    ) or any(
        phrase in normalized
        for phrase in (
            "unknown session",
            "unknown thread",
            "cannot resume",
            "could not resume",
            "unable to resume",
        )
    )


def _coerce_agent_result(
    result: tuple[int, str] | AgentResult,
    session_id: str | None,
) -> AgentResult:
    if isinstance(result, AgentResult):
        return result
    code, message = result
    return AgentResult(code, message, session_id)


def run_agent_with_retries(
    invoke: Callable[[str | None, bool], tuple[int, str] | AgentResult],
    *,
    session_id: str | None,
) -> AgentResult:
    """Retry transient CLI failures and replace an unavailable saved session once."""

    active_session_id = session_id
    transient_retries = 0
    session_fallback = False
    while True:
        try:
            raw_result = invoke(active_session_id, session_fallback)
            result = _coerce_agent_result(raw_result, active_session_id)
        except subprocess.TimeoutExpired as error:
            result = AgentResult(
                1,
                f"Agent CLI timeout: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )
        except OSError as error:
            result = AgentResult(
                1,
                f"Agent CLI 실행 실패: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )

        if result.session_id:
            active_session_id = result.session_id
        if result.result_kind is AgentResultKind.SUCCESS:
            return result
        if approval_policy_failure(result.message):
            return replace(result, kind=AgentResultKind.PERMANENT_CONFIGURATION)
        if isinstance(invoke, TrackedAgentCall):
            invoke.attempts.previous_failure = result.ledger_kind
        if (
            result.result_kind is not AgentResultKind.USAGE_LIMIT
            and active_session_id is not None
            and not session_fallback
            and session_resume_unavailable(result.message)
        ):
            active_session_id = None
            session_fallback = True
            events.emit("Agent 세션 재개 불가: 새 세션으로 전환", level="WARN",
                        reason="session_resume_unavailable", next_action="최소 문맥으로 재시도")
            continue
        if (
            result.result_kind is AgentResultKind.TRANSIENT_FAILURE
            and transient_retries < MAX_TRANSIENT_AGENT_RETRIES
        ):
            transient_retries += 1
            events.emit("Agent 일시 실패: 같은 실행에서 재시도", level="WARN",
                        reason=result.ledger_kind, retry=transient_retries,
                        retry_limit=MAX_TRANSIENT_AGENT_RETRIES)
            continue
        events.emit("Agent 호출 중단: 복구 판단으로 전달", level="WARN", reason=result.ledger_kind,
                    retries=transient_retries, retry_limit=MAX_TRANSIENT_AGENT_RETRIES)
        return result
