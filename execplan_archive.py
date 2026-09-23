"""Post-merge ExecPlan archive candidate validation and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Mapping, Protocol, Sequence

from execplan_archive_state import (
    ExecPlanArchiveRepository,
    MergedArchiveCandidate,
)


REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class ArchiveTreeEntry:
    mode: str
    object_type: str
    object_id: str
    content_hash: str

    @property
    def regular(self) -> bool:
        return self.object_type == "blob" and self.mode in REGULAR_FILE_MODES


@dataclass(frozen=True)
class ArchiveCandidateObservation:
    candidate: MergedArchiveCandidate
    source: ArchiveTreeEntry | None
    destination: ArchiveTreeEntry | None


@dataclass(frozen=True)
class ArchiveMove:
    plan_id: str
    source: str
    destination: str
    content_hash: str


@dataclass(frozen=True)
class PreparedArchive:
    operation_id: str
    base_sha: str
    final_hash: str
    commit_oid: str


@dataclass(frozen=True)
class ArchivePushResult:
    success: bool
    remote_oid: str | None = None
    error: str | None = None
    base_changed: bool = False


class ArchiveDispatchKind(str, Enum):
    NO_CANDIDATES = "no_candidates"
    CONVERGED = "converged"
    PENDING = "pending"
    TRANSIENT = "transient"


class ArchiveRemoteBaseChanged(RuntimeError):
    """The remote compare-and-swap base changed during commit preparation."""


@dataclass(frozen=True)
class ArchiveDispatchOutcome:
    kind: ArchiveDispatchKind
    archived_plan_ids: tuple[str, ...] = ()
    pending: tuple[tuple[str, str], ...] = ()
    commit_oid: str | None = None


class ExecPlanArchiveGitPort(Protocol):
    """Injected Git boundary; the archive domain never invokes subprocesses."""

    def fetch_remote_base(self, base_branch: str) -> str: ...

    def observe(
        self,
        revision: str,
        candidates: Sequence[MergedArchiveCandidate],
    ) -> tuple[ArchiveCandidateObservation, ...]: ...

    def prepare_commit(
        self,
        base_sha: str,
        base_branch: str,
        operation_id: str,
        moves: Sequence[ArchiveMove],
    ) -> PreparedArchive: ...

    def commit_matches(
        self,
        prepared: PreparedArchive,
        moves: Sequence[ArchiveMove],
    ) -> bool: ...

    def cleanup_prepared(self, prepared: PreparedArchive) -> None: ...

    def push_fast_forward(
        self,
        prepared: PreparedArchive,
        base_branch: str,
    ) -> ArchivePushResult: ...


def validate_candidate(
    observation: ArchiveCandidateObservation,
) -> tuple[str, ArchiveMove | None]:
    """Return ``safe``, ``archived`` or a stable candidate-level reason."""

    candidate = observation.candidate
    source = observation.source
    destination = observation.destination
    if source is None:
        if destination is None:
            return "source_missing", None
        if not destination.regular:
            return "archive_not_regular", None
        if destination.content_hash != candidate.content_hash:
            return "archive_hash_mismatch", None
        return "archived", None
    if destination is not None:
        return "source_and_destination_exist", None
    if not source.regular:
        return "source_not_regular", None
    if source.content_hash != candidate.content_hash:
        return "source_hash_mismatch", None
    return (
        "safe",
        ArchiveMove(
            plan_id=candidate.plan_id,
            source=candidate.active_path,
            destination=candidate.archive_path,
            content_hash=candidate.content_hash,
        ),
    )


def archive_operation_id(base_sha: str, moves: Sequence[ArchiveMove]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-execplan-archive-v1\0")
    digest.update(base_sha.encode("ascii"))
    digest.update(b"\0")
    for move in sorted(moves, key=lambda item: item.plan_id):
        for value in (
            move.plan_id,
            move.source,
            move.destination,
            move.content_hash,
        ):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


class ExecPlanArchiveDispatcher:
    """Archive every safe MERGED candidate without gating normal scheduling."""

    def __init__(
        self,
        repository: ExecPlanArchiveRepository,
        git: ExecPlanArchiveGitPort,
        *,
        max_base_restarts: int = 3,
    ):
        if max_base_restarts < 1:
            raise ValueError("max_base_restarts must be positive")
        self.repository = repository
        self.git = git
        self.max_base_restarts = max_base_restarts

    def dispatch(self, repo: str, base_branch: str) -> ArchiveDispatchOutcome:
        candidates = self.repository.merged_candidates(repo)
        if not candidates:
            return ArchiveDispatchOutcome(ArchiveDispatchKind.NO_CANDIDATES)

        last_pending: dict[str, str] = {}
        archived_ids: set[str] = set()
        for _attempt in range(self.max_base_restarts):
            try:
                base_sha = self.git.fetch_remote_base(base_branch)
                observations = self.git.observe(base_sha, candidates)
            except Exception as error:
                reason = f"remote_observation_failed:{error}"
                for candidate in candidates:
                    self.repository.record_pending(
                        repo, candidate, reason=reason
                    )
                    last_pending[candidate.plan_id] = reason
                return self._outcome(archived_ids, last_pending, transient=True)

            by_id = {item.candidate.plan_id: item for item in observations}
            if set(by_id) != {item.plan_id for item in candidates}:
                reason = "incomplete_candidate_observation"
                for candidate in candidates:
                    self.repository.record_pending(
                        repo, candidate, reason=reason, base_sha=base_sha
                    )
                    last_pending[candidate.plan_id] = reason
                return self._outcome(archived_ids, last_pending, transient=True)

            safe_candidates: list[MergedArchiveCandidate] = []
            moves: list[ArchiveMove] = []
            last_pending = {}
            for candidate in candidates:
                status, move = validate_candidate(by_id[candidate.plan_id])
                if status == "archived":
                    self.repository.record_success(
                        repo,
                        (candidate,),
                        base_sha=base_sha,
                        operation_id=None,
                        commit_oid=None,
                        push_result="observed_remote",
                    )
                    archived_ids.add(candidate.plan_id)
                elif status == "safe":
                    assert move is not None
                    safe_candidates.append(candidate)
                    moves.append(move)
                else:
                    self.repository.record_pending(
                        repo,
                        candidate,
                        reason=status,
                        base_sha=base_sha,
                    )
                    last_pending[candidate.plan_id] = status

            if not moves:
                return self._outcome(archived_ids, last_pending)

            operation_id = archive_operation_id(base_sha, moves)
            self.repository.record_operation_started(
                repo, operation_id, base_sha, safe_candidates
            )
            stored = self.repository.prepared_operation(repo, operation_id)
            prepared: PreparedArchive | None = None
            if stored is not None:
                recovered = PreparedArchive(
                    stored.operation_id,
                    stored.base_sha,
                    stored.final_hash,
                    stored.commit_oid,
                )
                try:
                    if self.git.commit_matches(recovered, moves):
                        prepared = recovered
                except Exception:
                    prepared = None

            if prepared is None:
                try:
                    commit_base = self.git.fetch_remote_base(base_branch)
                except Exception as error:
                    reason = f"pre_commit_base_check_failed:{error}"
                    self.repository.record_operation_pending(
                        repo,
                        operation_id,
                        safe_candidates,
                        reason=reason,
                        push_result="pre_commit_check_failed",
                        base_sha=base_sha,
                    )
                    last_pending.update(
                        (candidate.plan_id, reason)
                        for candidate in safe_candidates
                    )
                    return self._outcome(
                        archived_ids, last_pending, transient=True
                    )
                if commit_base != base_sha:
                    self.repository.record_operation_pending(
                        repo,
                        operation_id,
                        safe_candidates,
                        reason="remote_base_changed_before_commit",
                        push_result="base_changed",
                        base_sha=base_sha,
                    )
                    continue
                try:
                    prepared = self.git.prepare_commit(
                        base_sha, base_branch, operation_id, moves
                    )
                    if not self.git.commit_matches(prepared, moves):
                        raise RuntimeError(
                            "prepared archive commit failed rename-only validation"
                        )
                except ArchiveRemoteBaseChanged:
                    self.repository.record_operation_pending(
                        repo,
                        operation_id,
                        safe_candidates,
                        reason="remote_base_changed_before_commit",
                        push_result="base_changed",
                        base_sha=base_sha,
                    )
                    continue
                except Exception as error:
                    reason = f"commit_preparation_failed:{error}"
                    self.repository.record_operation_pending(
                        repo,
                        operation_id,
                        safe_candidates,
                        reason=reason,
                        push_result="prepare_failed",
                        base_sha=base_sha,
                    )
                    last_pending.update(
                        (candidate.plan_id, reason)
                        for candidate in safe_candidates
                    )
                    return self._outcome(
                        archived_ids, last_pending, transient=True
                    )
                self.repository.record_prepared(
                    repo,
                    operation_id,
                    base_sha,
                    prepared.final_hash,
                    prepared.commit_oid,
                    safe_candidates,
                )

            try:
                self.git.cleanup_prepared(prepared)
            except Exception as error:
                reason = f"workspace_cleanup_failed:{error}"
                self.repository.record_operation_pending(
                    repo,
                    operation_id,
                    safe_candidates,
                    reason=reason,
                    push_result="workspace_cleanup_failed",
                    base_sha=base_sha,
                    commit_oid=prepared.commit_oid,
                    final_hash=prepared.final_hash,
                )
                last_pending.update(
                    (candidate.plan_id, reason) for candidate in safe_candidates
                )
                return self._outcome(archived_ids, last_pending, transient=True)

            try:
                push_base = self.git.fetch_remote_base(base_branch)
            except Exception as error:
                reason = f"pre_push_base_check_failed:{error}"
                self.repository.record_operation_pending(
                    repo,
                    operation_id,
                    safe_candidates,
                    reason=reason,
                    push_result="pre_push_check_failed",
                    base_sha=base_sha,
                    commit_oid=prepared.commit_oid,
                    final_hash=prepared.final_hash,
                )
                last_pending.update(
                    (candidate.plan_id, reason) for candidate in safe_candidates
                )
                return self._outcome(archived_ids, last_pending, transient=True)
            if push_base != base_sha:
                self.repository.record_operation_pending(
                    repo,
                    operation_id,
                    safe_candidates,
                    reason="remote_base_changed_before_push",
                    push_result="base_changed",
                    base_sha=base_sha,
                    commit_oid=prepared.commit_oid,
                    final_hash=prepared.final_hash,
                )
                continue

            try:
                pushed = self.git.push_fast_forward(prepared, base_branch)
            except Exception as error:
                pushed = ArchivePushResult(False, error=str(error))
            if pushed.success:
                self.repository.record_success(
                    repo,
                    safe_candidates,
                    base_sha=base_sha,
                    operation_id=operation_id,
                    commit_oid=prepared.commit_oid,
                    push_result=f"pushed:{pushed.remote_oid or prepared.commit_oid}",
                )
                archived_ids.update(item.plan_id for item in safe_candidates)
                return self._outcome(
                    archived_ids, last_pending, commit_oid=prepared.commit_oid
                )
            if pushed.base_changed:
                self.repository.record_operation_pending(
                    repo,
                    operation_id,
                    safe_candidates,
                    reason="remote_base_changed_during_push",
                    push_result="base_changed",
                    base_sha=base_sha,
                    commit_oid=prepared.commit_oid,
                    final_hash=prepared.final_hash,
                )
                continue

            reason = f"push_failed:{pushed.error or 'unknown error'}"
            self.repository.record_operation_pending(
                repo,
                operation_id,
                safe_candidates,
                reason=reason,
                push_result="push_failed",
                base_sha=base_sha,
                commit_oid=prepared.commit_oid,
                final_hash=prepared.final_hash,
            )
            last_pending.update(
                (candidate.plan_id, reason) for candidate in safe_candidates
            )
            return self._outcome(archived_ids, last_pending, transient=True)

        reason = "remote_base_changed_restart_limit"
        for candidate in candidates:
            if candidate.plan_id in archived_ids or candidate.plan_id in last_pending:
                continue
            self.repository.record_pending(repo, candidate, reason=reason)
            last_pending[candidate.plan_id] = reason
        return self._outcome(archived_ids, last_pending, transient=True)

    @staticmethod
    def _outcome(
        archived_ids: set[str],
        pending: Mapping[str, str],
        *,
        transient: bool = False,
        commit_oid: str | None = None,
    ) -> ArchiveDispatchOutcome:
        if transient:
            kind = ArchiveDispatchKind.TRANSIENT
        elif pending:
            kind = ArchiveDispatchKind.PENDING
        else:
            kind = ArchiveDispatchKind.CONVERGED
        return ArchiveDispatchOutcome(
            kind,
            tuple(sorted(archived_ids)),
            tuple(sorted(pending.items())),
            commit_oid,
        )
