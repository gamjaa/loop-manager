"""Provider-neutral, non-secret observations for physical Agent CLI attempts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from agent_write_isolation import IsolationEvidence


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


def classify_agent_result(code: int, message: str) -> AgentResultKind:
    if code == 0:
        return AgentResultKind.SUCCESS
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
                and "timeout" in self.message.casefold()):
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


class AttemptSequence:
    """Assign physical attempt order without sharing mutable process state."""

    def __init__(self, repo: str, plan_id: str | None, revision: str | None = None,
                 initial_transition: str | None = None,
                 initial_cause: str | None = None):
        self.repo = repo
        self.plan_id = plan_id
        self.revision = revision
        self.initial_transition = initial_transition
        self.initial_cause = initial_cause
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
            self.repo, self.plan_id, self.revision, transition, cause, self.count
        )


class TrackedAgentCall:
    """Callable that supplies one context for each retry runner attempt."""

    def __init__(self, invoke: Callable[[str | None, bool, InvocationContext], object],
                 attempts: AttemptSequence):
        self.invoke = invoke
        self.attempts = attempts

    def __call__(self, session_id: str | None, fallback: bool):
        return self.invoke(session_id, fallback, self.attempts.next(session_id, fallback))
