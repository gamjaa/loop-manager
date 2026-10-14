"""Policy and dispatcher for remote checkpoints of safely blocked ExecPlans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from typing import Protocol


class CheckpointPhase(str, Enum):
    REQUESTED = "requested"
    PREPARED = "prepared"
    LOCAL_COMMITTED = "local_committed"
    RETRY_PENDING = "retry_pending"
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"


class SafetyScope(str, Enum):
    PLAN = "plan"
    PROJECT = "project"


class PushMode(str, Enum):
    NONE = "none"
    CREATE = "create"
    FAST_FORWARD = "fast_forward"
    REBASE_LEASE = "rebase_lease"
    REJECT = "reject"


@dataclass(frozen=True)
class CheckpointRequest:
    repo: str
    plan_id: str
    request_identity: str
    phase: CheckpointPhase
    reason_code: str
    evidence: str
    branch: str | None
    worktree: str | None
    expected_remote_oid: str | None = None
    remote_observed: bool = False
    commit_identity: str | None = None
    tree_oid: str | None = None
    local_oid: str | None = None
    remote_oid: str | None = None
    rebase_recovery: bool = False
    pr_url: str | None = None


@dataclass(frozen=True)
class DirtyPath:
    path: str
    kinds: tuple[str, ...]
    allowed: bool
    symlink: bool = False
    ignored: bool = False
    index_flags: tuple[str, ...] = ()
    external_worktree: bool = False


@dataclass(frozen=True)
class SafetyObservation:
    workspace_identity_matches: bool
    workspace_owned: bool
    head_oid: str | None
    actual_branch: str | None
    expected_branch: str | None
    operations: tuple[str, ...] = ()
    dirty_paths: tuple[DirtyPath, ...] = ()
    shared_metadata_owned: bool = True
    project_blocked: bool = False
    project_recovery_verified: bool = False
    legacy_scope_uncertain: bool = False
    agent_scope_recovery_pending: bool = False


@dataclass(frozen=True)
class SafetyVerdict:
    safe: bool
    scope: SafetyScope
    reason_code: str | None
    detail: str | None
    head_oid: str | None
    included_paths: tuple[str, ...]


@dataclass(frozen=True)
class PreparedCheckpoint:
    parent_oid: str
    tree_oid: str
    commit_identity: str
    message: str
    included_paths: tuple[str, ...]


@dataclass(frozen=True)
class PushDecision:
    mode: PushMode
    reason: str | None = None


class CheckpointPushError(RuntimeError):
    """A push failed while preserving whether another run may retry it."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def assess_safety(observation: SafetyObservation) -> SafetyVerdict:
    """Return a typed verdict without reading SQLite or parsing free-form errors."""

    if observation.project_blocked and not observation.project_recovery_verified:
        return SafetyVerdict(
            False,
            SafetyScope.PROJECT,
            "project_blocked",
            "프로젝트 안전 차단 상태에서는 checkpoint를 게시하지 않습니다.",
            observation.head_oid,
            (),
        )
    if observation.legacy_scope_uncertain:
        return SafetyVerdict(
            False,
            SafetyScope.PROJECT,
            "legacy_scope_uncertain",
            "legacy 차단의 계획 범위와 소유권을 증명할 수 없습니다.",
            observation.head_oid,
            (),
        )
    if not observation.workspace_identity_matches or not observation.workspace_owned:
        return SafetyVerdict(
            False,
            SafetyScope.PROJECT,
            "workspace_ownership_uncertain",
            "저장된 branch, worktree와 workspace 소유권이 일치하지 않습니다.",
            observation.head_oid,
            (),
        )
    if not observation.shared_metadata_owned:
        return SafetyVerdict(
            False,
            SafetyScope.PROJECT,
            "workspace_ownership_uncertain",
            "현재 작업 branch와 ref·worktree 등록 연결을 확인할 수 없습니다.",
            observation.head_oid,
            (),
        )
    if observation.agent_scope_recovery_pending:
        return SafetyVerdict(
            False,
            SafetyScope.PLAN,
            "agent_scope_recovery_pending",
            "Agent 범위 위반 변경의 격리가 끝나기 전에는 checkpoint를 게시하지 않습니다.",
            observation.head_oid,
            (),
        )
    if not observation.head_oid or observation.actual_branch is None:
        return SafetyVerdict(
            False,
            SafetyScope.PLAN,
            "detached_head",
            "관리 worktree의 HEAD를 확인할 수 없거나 detached HEAD입니다.",
            observation.head_oid,
            (),
        )
    if observation.actual_branch != observation.expected_branch:
        return SafetyVerdict(
            False,
            SafetyScope.PLAN,
            "wrong_branch",
            "관리 worktree가 저장된 ExecPlan branch에 있지 않습니다.",
            observation.head_oid,
            (),
        )
    if observation.operations:
        return SafetyVerdict(
            False,
            SafetyScope.PLAN,
            "git_operation_in_progress",
            "미해결 Git operation: " + ", ".join(observation.operations),
            observation.head_oid,
            (),
        )

    violations: list[str] = []
    for item in observation.dirty_paths:
        problems = []
        if not item.allowed:
            problems.append("허용 범위 밖")
        if item.symlink:
            problems.append("symlink")
        if item.ignored:
            problems.append("ignored")
        if item.index_flags:
            problems.append("index flags " + ",".join(item.index_flags))
        if item.external_worktree:
            problems.append("외부 worktree 또는 특수 파일")
        if problems:
            violations.append(f"{item.path} ({', '.join(problems)})")
    if violations:
        return SafetyVerdict(
            False,
            SafetyScope.PLAN,
            "dirty_scope_violation",
            "checkpoint 변경 범위 위반: " + "; ".join(violations),
            observation.head_oid,
            (),
        )
    return SafetyVerdict(
        True,
        SafetyScope.PLAN,
        None,
        None,
        observation.head_oid,
        tuple(sorted(item.path for item in observation.dirty_paths)),
    )


def checkpoint_identity(
    *,
    plan_id: str,
    reason_code: str,
    evidence: str,
    parent_oid: str,
    tree_oid: str,
    included_paths: tuple[str, ...],
) -> str:
    encoded = json.dumps(
        {
            "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
            "parent_oid": parent_oid,
            "paths": sorted(included_paths),
            "plan_id": plan_id,
            "reason_code": reason_code,
            "scope": "plan",
            "tree_oid": tree_oid,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def checkpoint_message(
    *,
    plan_id: str,
    reason_code: str,
    evidence: str,
    identity: str,
    included_paths: tuple[str, ...],
) -> str:
    paths = "\n".join(f"- {path}" for path in sorted(included_paths)) or "- (none)"
    return (
        f"[{plan_id}] BLOCKED checkpoint\n\n"
        "Scope: plan\n"
        f"Reason: {reason_code}\n"
        f"Evidence: {json.dumps(evidence, ensure_ascii=True)}\n"
        f"Evidence-SHA256: {hashlib.sha256(evidence.encode()).hexdigest()}\n"
        f"Checkpoint-Identity: {identity}\n"
        f"Paths:\n{paths}\n"
    )


def checkpoint_pr_matches(pull_request, *, url, branch, head, base=None) -> bool:
    """Bind an existing open PR to the selected branch and exact live ref."""
    if not url:
        return pull_request is None
    return bool(
        isinstance(pull_request, dict) and head
        and pull_request.get('url') == url
        and pull_request.get('state') == 'OPEN' and not pull_request.get('mergedAt')
        and pull_request.get('headRefName') == branch
        and pull_request.get('headRefOid') == head
        and (base is None or pull_request.get('baseRefName') == base)
    )


def decide_push(
    *,
    local_oid: str,
    remote_oid: str | None,
    expected_remote_oid: str | None,
    remote_observed: bool,
    fast_forward: bool,
    rebase_recovery: bool,
) -> PushDecision:
    """Grant only create, fast-forward, or the existing narrow rebase lease."""

    if remote_oid == local_oid:
        return PushDecision(PushMode.NONE)
    if remote_oid is None:
        if remote_observed and expected_remote_oid is not None:
            return PushDecision(
                PushMode.REJECT,
                "관측했던 원격 checkpoint branch가 삭제됐습니다.",
            )
        return PushDecision(PushMode.CREATE)
    if not remote_observed or expected_remote_oid != remote_oid:
        return PushDecision(
            PushMode.REJECT,
            "원격 branch OID가 저장된 expected OID와 일치하지 않습니다.",
        )
    if fast_forward:
        return PushDecision(PushMode.FAST_FORWARD)
    if rebase_recovery:
        return PushDecision(PushMode.REBASE_LEASE)
    return PushDecision(
        PushMode.REJECT,
        "원격 branch를 fast-forward할 수 없어 checkpoint 갱신을 거부합니다.",
    )


def diagnostic_branch(repo: str, plan_id: str, branch: str, base: str, head: str, tree: str) -> str:
    """Name one immutable artifact independently of the original publication."""
    identity = hashlib.sha256(json.dumps(
        [repo, plan_id, branch, base, head, tree], separators=(",", ":"),
    ).encode()).hexdigest()
    return f"loop-manager/diagnostics/{plan_id}/{identity}"


def decide_diagnostic_push(local_oid: str, remote_oid: str | None) -> PushDecision:
    if remote_oid == local_oid:
        return PushDecision(PushMode.NONE)
    if remote_oid is None:
        return PushDecision(PushMode.CREATE)
    return PushDecision(PushMode.REJECT, "진단 ref에 다른 OID가 있어 갱신하지 않습니다.")


def push_failure_retryable(output: str) -> bool:
    normalized = ' '.join(output.casefold().split())
    return not any(marker in normalized for marker in (
        'protected branch', 'permission denied', 'not permitted',
        'authentication failed', 'authorization failed', 'http 403',
    ))


class CheckpointStatePort(Protocol):
    def pending(self, repo: str) -> tuple[CheckpointRequest, ...]: ...
    def record_prepared(self, request: CheckpointRequest, prepared: PreparedCheckpoint) -> None: ...
    def record_local(self, request: CheckpointRequest, local_oid: str) -> None: ...
    def record_remote_observation(self, request: CheckpointRequest, remote_oid: str | None) -> CheckpointRequest: ...
    def finish_published(self, request: CheckpointRequest, remote_oid: str, *, reobserve=None) -> None: ...
    def finish_unpublished(self, request: CheckpointRequest, reason: str, *, retryable: bool) -> None: ...


class CheckpointSafetyPort(Protocol):
    def assess(self, request: CheckpointRequest) -> SafetyVerdict: ...


class CheckpointPublicationPort(Protocol):
    def existing_pull_request(self, request: CheckpointRequest) -> object | None: ...
    def confirm(self, request: CheckpointRequest, local_oid: str) -> str | None: ...
    def prepare(self, request: CheckpointRequest, verdict: SafetyVerdict) -> PreparedCheckpoint: ...
    def ensure_local_commit(self, request: CheckpointRequest, prepared: PreparedCheckpoint) -> str: ...
    def observe_remote(self, request: CheckpointRequest) -> str | None: ...
    def is_fast_forward(self, remote_oid: str, local_oid: str) -> bool: ...
    def push(self, request: CheckpointRequest, local_oid: str, decision: PushDecision) -> None: ...


class CheckpointResultPort(Protocol):
    def promote_project_block(self, request: CheckpointRequest, verdict: SafetyVerdict) -> None: ...
    def finalized(self, request: CheckpointRequest) -> None: ...


class BlockedCheckpointDispatcher:
    """Process durable requests outside the active execution coordinator."""

    def __init__(
        self,
        state: CheckpointStatePort,
        safety: CheckpointSafetyPort,
        publication: CheckpointPublicationPort,
        results: CheckpointResultPort,
        *,
        max_push_attempts: int = 3,
    ):
        self._state = state
        self._safety = safety
        self._publication = publication
        self._results = results
        self._max_push_attempts = max_push_attempts

    def dispatch(
        self,
        repo: str,
        *,
        skip_identities: frozenset[str] = frozenset(),
    ) -> int:
        completed = 0
        for request in self._state.pending(repo):
            if request.request_identity in skip_identities:
                continue
            if self._dispatch_one(request):
                completed += 1
        return completed

    def _finish_published(self, request, local_oid):
        reconcile = getattr(type(self._state), 'reconcile_cause', None)
        if reconcile is not None:
            current = reconcile(self._state, request)
            if current is None or current.request_identity != request.request_identity:
                return
        if request.pr_url:
            self._state.finish_published(
                request, local_oid, reobserve=lambda: self._publication.confirm(request, local_oid))
        else:
            self._state.finish_published(request, local_oid)

    def _dispatch_one(self, request: CheckpointRequest) -> bool:
        reconcile = getattr(type(self._state), 'reconcile_cause', None)
        if reconcile is not None:
            request = reconcile(self._state, request)
            if request is None:
                return True
        verdict = self._safety.assess(request)
        if reconcile is not None and verdict.reason_code == 'checkpoint_identity_changed':
            refreshed = reconcile(self._state, request)
            if refreshed is None:
                return True
            if refreshed.request_identity != request.request_identity:
                request = refreshed
                verdict = self._safety.assess(request)
        if not verdict.safe:
            if (
                verdict.scope is SafetyScope.PROJECT
                and verdict.reason_code != "project_blocked"
            ):
                self._results.promote_project_block(request, verdict)
            self._state.finish_unpublished(
                request,
                verdict.detail or "checkpoint 안전성을 증명할 수 없습니다.",
                retryable=False,
            )
            self._results.finalized(request)
            return True

        if reconcile is not None:
            current = self._state.get(request.repo, request.plan_id)
            if current.request_identity != request.request_identity:
                return True
            request = current
        pull_request = self._publication.existing_pull_request(request)
        if not any(checkpoint_pr_matches(
                pull_request, url=request.pr_url, branch=request.branch, head=head)
                for head in (request.expected_remote_oid, request.local_oid, verdict.head_oid)):
            self._state.finish_unpublished(
                request,
                "기존 PR identity, 상태 또는 head가 checkpoint 대상과 다릅니다.",
                retryable=False,
            )
            self._results.finalized(request)
            return True

        prepared = self._publication.prepare(request, verdict)
        self._state.record_prepared(request, prepared)
        local_oid = self._publication.ensure_local_commit(request, prepared)
        self._state.record_local(request, local_oid)
        if request.pr_url:
            request = replace(request, local_oid=local_oid, tree_oid=prepared.tree_oid,
                              commit_identity=prepared.commit_identity)
        remote_oid = self._publication.observe_remote(request)
        request = self._state.record_remote_observation(request, remote_oid)
        decision = decide_push(
            local_oid=local_oid,
            remote_oid=remote_oid,
            expected_remote_oid=request.expected_remote_oid,
            remote_observed=request.remote_observed,
            fast_forward=(
                remote_oid is not None
                and request.remote_observed
                and request.expected_remote_oid == remote_oid
                and self._publication.is_fast_forward(remote_oid, local_oid)
            ),
            rebase_recovery=request.rebase_recovery,
        )
        if decision.mode is PushMode.REJECT:
            self._state.finish_unpublished(
                request,
                decision.reason or "원격 checkpoint 갱신이 거부됐습니다.",
                retryable=False,
            )
            self._results.finalized(request)
            return True
        if decision.mode is PushMode.NONE:
            self._finish_published(request, local_oid)
            self._results.finalized(request)
            return True

        last_error: CheckpointPushError | None = None
        for _ in range(self._max_push_attempts):
            try:
                self._publication.push(request, local_oid, decision)
                confirmed = self._publication.observe_remote(request)
                if confirmed != local_oid:
                    raise CheckpointPushError(
                        "checkpoint push 결과를 원격에서 확인할 수 없습니다.",
                        retryable=True,
                    )
                self._finish_published(request, local_oid)
                self._results.finalized(request)
                return True
            except CheckpointPushError as error:
                last_error = error
                if not error.retryable:
                    break
        assert last_error is not None
        self._state.finish_unpublished(
            request,
            str(last_error),
            retryable=last_error.retryable,
        )
        self._results.finalized(request)
        return True
