"""Archive execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from dataclasses import dataclass
from typing import Callable, Any
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure
from loop_manager_internal.storage.requirement_archive_state import ArchiveTargetRepository, ArchiveTargetConflict

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class ArchiveHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    contract: ExecutionContract
    state: ExecutionStatePort
    git: Callable[..., Any]
    requirement_archive_candidates: Callable[..., Any]
    requirement_archive_snapshot: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _recover: Callable[..., ExecutionOutcome]


class ArchiveHandler:
    """Archive policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: ArchiveHandlerPorts):
        self.ports = ports

    def _archive_collision(self, context, error):
        return self.ports._recover(
            RecoveryReason.PLAN_INPUT_INVALID, str(error), context=context,
            user_targets=(error.path,),
        )

    def _archive_workspace_owned(self, context):
        active = self.ports.state.active_execplan(self.ports.identity.repo)
        if not (
            active is not None
            and str(active['plan_id']) == context.plan.plan_id
            and int(active['workspace_owned']) == self.ports.contract.WORKSPACE_OWNED
            and str(active['branch'] or '') == context.branch
            and str(active['worktree'] or '') == str(context.worktree)
            and context.plan.plan_sha
        ):
            return False
        try:
            self.ports.validate_managed_worktree(
                self.ports.identity.repo_path, context.worktree, context.branch
            )
            return self.ports.git(
                context.worktree, 'merge-base', '--is-ancestor',
                context.plan.plan_sha, 'HEAD', check=False,
            ).returncode == 0
        except self.ports.contract.WorkspaceOwnershipError:
            return False
        except self.ports.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.STATUS_FAILED, str(error)) from error

    def _archive_candidates_at(self, context, revision):
        """Require the archive input used by this phase before reading moves."""

        try:
            if self.ports.snapshot()._archive_snapshot is None:
                return self.ports.requirement_archive_candidates(
                    self.ports.identity.repo_path, self.ports.identity.repo, revision, str(self.ports.snapshot().plan['id'])
                )
            snapshot = self.ports.requirement_archive_snapshot(
                self.ports.identity.repo_path, self.ports.identity.repo, revision, str(self.ports.snapshot().plan['id'])
            )
        except self.ports.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        if (
            snapshot.token != self.ports.snapshot()._archive_snapshot.token
            or snapshot.token != context.archive_snapshot_token
        ):
            raise RecoveryFailure(
                RecoveryReason.CATALOG_CACHE_STALE,
                'Requirement archive base/catalog/state snapshot이 변경됐습니다.',
            )
        return snapshot.candidates

    def _record_archive_target(self, stage, *, moves=None):
        if self.ports.snapshot()._archive_snapshot is None:
            return
        snapshot = (
            self.ports.snapshot()._archive_snapshot.with_moves(tuple(moves))
            if moves is not None else self.ports.snapshot()._archive_snapshot
        )
        repository = ArchiveTargetRepository(self.ports.state.connection)
        previous = repository.get(self.ports.identity.repo, str(self.ports.snapshot().plan['id']))
        try:
            repository.put(
                self.ports.identity.repo, snapshot,
                expected_execution=self.ports.state.expected,
                expected_generation=previous.generation if previous else None,
                base_is_current=lambda oid: self.ports._fetch_latest_base() == oid,
                snapshot_is_current=lambda: (
                    self.ports.requirement_archive_snapshot(
                        self.ports.identity.repo_path, self.ports.identity.repo, snapshot.base_oid,
                        str(self.ports.snapshot().plan['id']),
                    ).token == snapshot.token
                ),
                stage=stage,
            )
        except ArchiveTargetConflict as error:
            raise RecoveryFailure(
                RecoveryReason.REMOTE_SNAPSHOT_RACE,
                'Requirement archive target 저장 중 snapshot이 변경됐습니다.',
            ) from error
        except self.ports.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except self.ports.contract.RequirementArchiveError as error:
            raise RecoveryFailure(RecoveryReason.CATALOG_INVALID, str(error)) from error

