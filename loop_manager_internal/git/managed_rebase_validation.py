"""Current Git result acceptance, independent of rebase execution evidence.

This boundary does not authorize publication or reconcile Agent scope incidents.
Its observation adapter lives in managed_rebase_observation; execution and
direct recovery consume it through managed_rebase_adoption.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Callable, Mapping

from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation, owned_detached_rebase


POLICY_VERSION = 1
ACCEPTANCE_KIND = 'current_state_rebase_acceptance'
_MODES = frozenset({'000000', '100644', '100755', '120000', '160000'})


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class PullRequest:
    identity: str
    repository: str
    branch: str
    head: str
    state: str


@dataclass(frozen=True)
class Remote:
    """None OID/PR means confirmed absence; a missing Remote means unknown."""

    repository: str
    branch: str
    oid: str | None
    pull_request: PullRequest | None = None


@dataclass(frozen=True)
class RebaseExpectation:
    repo: str
    repository_identity: str
    plan_id: str
    plan_path: str
    worktree: str
    branch: str
    target_base_oid: str
    generation: int
    workspace_owned: bool
    input_fingerprint: str
    scope_fingerprint: str
    remote: Remote | None
    before_head: str | None = None
    role: str = 'worker'
    autostash_oid: str | None = None


@dataclass(frozen=True)
class FileChange:
    path: str
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    status: str


@dataclass(frozen=True)
class IndexEntry:
    path: str
    mode: str
    oid: str
    stage: int
    tag: str
    flags: int


@dataclass(frozen=True)
class WorktreeFile:
    path: str
    kinds: tuple[str, ...]
    mode: str
    signature: str


@dataclass(frozen=True)
class RebaseObservation:
    repo: str
    repository_identity: str
    worktree: str
    identity_verified: bool
    head: str
    branch: str | None
    branch_ref: tuple[str, str] | None
    registration: tuple[str, ...] | None
    branch_worktrees: tuple[str, ...]
    operations: tuple[tuple[str, str], ...]
    metadata: tuple[tuple[str, str], ...] | None
    base_is_ancestor: bool | None
    index_clean: bool | None
    index: tuple[IndexEntry, ...]
    committed: tuple[FileChange, ...]
    staged: tuple[FileChange, ...]
    files: tuple[WorktreeFile, ...]
    remote: Remote | None
    input_fingerprint: str
    scope_fingerprint: str
    # Only the selected reservation and consumed origin/ignore inputs affect stability.
    repository_state_fingerprint: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class ObservationFailure:
    area: str
    detail: str


@dataclass(frozen=True)
class ScopeResult:
    allowed: bool
    input_fingerprint: str
    policy_fingerprint: str
    state_fingerprint: str
    violations: tuple[str, ...] = ()


def _safe_path(path: str) -> bool:
    return bool(path and not path.startswith(('/', '\\'))
                and '\\' not in path and '\0' not in path
                and ':' not in path and all(
                    part.casefold() not in {'', '.', '..', '.git'} for part in path.split('/')))


def rebase_content_pending(expected: RebaseExpectation, observed: RebaseObservation) -> bool:
    """The reserved start and replay/restoration bytes are not a final candidate."""
    return bool(observed.metadata is not None
                or {'rebase-merge', 'rebase-apply'}.intersection(dict(observed.operations))
                or observed.base_is_ancestor is False
                or (expected.autostash_oid
                    and (observed.staged or any(entry.stage for entry in observed.index))))


def assess_scope(
    expected: RebaseExpectation, observed: RebaseObservation, *,
    path_allowed: Callable[[str], bool],
    contracts_allowed: Callable[[RebaseObservation], bool],
) -> ScopeResult:
    """Check final changes, never original commits or historical stage trees.

    path_allowed reuses the current role/selected-plan path policy. The mandatory
    contracts_allowed checks exact approved guidance and content-preserving
    archive changes, including remaining working files, against current inputs.
    These callbacks are pure and must not query Git or mutate state.
    """
    violations = []
    content_pending = rebase_content_pending(expected, observed)
    paths = {change.path for change in (*observed.committed, *observed.staged)}
    paths.update(file.path for file in observed.files)
    paths.update(entry.path for entry in observed.index if entry.stage)
    for path in sorted(paths):
        if not _safe_path(path) or (not content_pending and not path_allowed(path)):
            violations.append(f'path:{path}')
    for change in (*observed.committed, *observed.staged):
        if (change.old_mode not in _MODES or change.new_mode not in _MODES
                or change.status not in {'A', 'D', 'M', 'T', 'U'}):
            violations.append(f'mode:{change.path}')
    for file in observed.files:
        if (file.mode not in _MODES or not file.signature or not file.kinds
                or not set(file.kinds) <= {
                    'staged', 'unstaged', 'untracked', 'ignored', 'index-hidden',
                }):
            violations.append(f'file:{file.path}')
    seen = set()
    for entry in observed.index:
        key = (entry.path, entry.stage)
        if (not _safe_path(entry.path) or entry.mode not in _MODES - {'000000'}
                or not entry.oid or entry.stage not in {0, 1, 2, 3}
                or key in seen or entry.flags < 0
                # Debug flags include stage bits. All other flags are rejected,
                # including assume-unchanged, skip-worktree and intent-to-add.
                or entry.flags & ~0x3000
                or (entry.flags & 0x3000) != entry.stage << 12
                or entry.tag != ('H' if entry.stage == 0 else 'M')):
            violations.append(f'index:{entry.path}')
        seen.add(key)
    if any((path, 0) in seen and any((path, stage) in seen for stage in (1, 2, 3))
           for path, _stage in seen):
        violations.append('index:mixed_stages')
    if not content_pending and not contracts_allowed(observed):
        violations.append('current_guidance_or_archive_contract')
    return ScopeResult(
        not violations, expected.input_fingerprint, expected.scope_fingerprint,
        observed.fingerprint, tuple(violations),
    )


class RebaseStatus(str, Enum):
    START = 'ready_to_start'
    WAITING = 'waiting_for_resolution'
    COMPLETE = 'complete'
    REJECTED = 'rejected'
    UNAVAILABLE = 'observation_unavailable'


@dataclass(frozen=True)
class RebaseDecision:
    status: RebaseStatus
    reason: str = ''
    acceptance: dict | None = None


def _known_remote(remote: Remote | None, branch: str) -> bool:
    if (not isinstance(remote, Remote)
            or not isinstance(remote.repository, str) or not remote.repository
            or remote.branch != branch):
        return False
    if remote.oid is not None and (not isinstance(remote.oid, str) or not remote.oid):
        return False
    pr = remote.pull_request
    return pr is None or bool(
        isinstance(pr, PullRequest) and isinstance(pr.identity, str) and pr.identity
        and pr.repository == remote.repository and pr.branch == branch
        and pr.head and pr.head == remote.oid and pr.state == 'OPEN'
    )


def assess_current_rebase(
    expected: RebaseExpectation,
    observed: RebaseObservation | ObservationFailure | None,
    scope: ScopeResult | None,
    *, adopted_head: str | None = None,
) -> RebaseDecision:
    """Distinguish an owned pending operation from an acceptable final result."""
    def reject(reason: str) -> RebaseDecision:
        return RebaseDecision(RebaseStatus.REJECTED, reason)

    if (not all(isinstance(value, str) and value for value in (
                expected.repo, expected.repository_identity, expected.worktree,
                expected.branch, expected.target_base_oid, expected.plan_id,
                expected.plan_path, expected.input_fingerprint, expected.scope_fingerprint))
            or re.fullmatch(r'G-\d{3,}', expected.plan_id) is None
            or re.fullmatch(r'docs/plans/\[' + expected.plan_id + r'\] [^/]+\.md',
                            expected.plan_path) is None
            or not _safe_path(expected.plan_path)
            or not expected.plan_path.endswith('.md')
            or type(expected.generation) is not int or expected.generation < 0
            or expected.role != 'worker'
            or expected.workspace_owned is not True
            or not _known_remote(expected.remote, expected.branch)):
        return reject('expectation_or_reservation_unavailable')
    if observed is None or isinstance(observed, ObservationFailure):
        detail = observed.area if observed is not None else 'missing'
        return RebaseDecision(RebaseStatus.UNAVAILABLE, detail)
    if scope is None or observed.remote is None:
        return RebaseDecision(RebaseStatus.UNAVAILABLE, 'scope_or_remote_missing')
    if observed.index_clean is None:
        return RebaseDecision(RebaseStatus.UNAVAILABLE, 'index_missing')
    if (not observed.identity_verified or observed.repo != expected.repo
            or observed.repository_identity != expected.repository_identity
            or observed.worktree != expected.worktree or not observed.head
            or not observed.repository_state_fingerprint
            or observed.input_fingerprint != expected.input_fingerprint
            or observed.scope_fingerprint != expected.scope_fingerprint):
        return reject('workspace_or_input_identity_mismatch')
    if observed.remote != expected.remote:
        return reject('remote_or_pr_lease_mismatch')
    if (scope.allowed is not True or scope.violations
            or scope.input_fingerprint != expected.input_fingerprint
            or scope.policy_fingerprint != expected.scope_fingerprint
            or scope.state_fingerprint != observed.fingerprint):
        return reject('current_scope_mismatch')
    registration = set(observed.registration or ())
    if (f'HEAD={observed.head}' not in registration
            or any(item.startswith('prunable=') for item in registration)
            or observed.branch_ref is None or observed.branch_ref[1]):
        return reject('registration_or_owned_ref_mismatch')
    operations = dict(observed.operations)
    rebase_markers = {'rebase-merge', 'rebase-apply'}.intersection(operations)
    if observed.metadata is not None or rebase_markers:
        if (len(rebase_markers) != 1 or observed.metadata is None
                or controlling_git_operation({
                    key: value for key, value in operations.items()
                    if key not in rebase_markers
                }) or observed.branch is not None
                or 'detached=True' not in registration
                or any(item.startswith('branch=') for item in registration)
                or observed.branch_worktrees
                or observed.branch_ref[0] != expected.before_head):
            return reject('rebase_operation_mismatch')
        metadata = dict(observed.metadata)
        # The common detached ownership helper requires a published remote.
        # For a confirmed absent branch, use the same metadata relationships.
        remote_oid = expected.remote.oid
        owned = owned_detached_rebase(
            operation='rebase', expected_head=expected.before_head,
            target_base_oid=expected.target_base_oid,
            recorded_before_head=expected.before_head, branch=expected.branch,
            remote_oid=remote_oid, accepted_remote_oid=remote_oid,
            recorded_remote_oid=remote_oid, checkpoint_remote_oid=remote_oid,
            head_name=metadata.get('head-name'), orig_head=metadata.get('orig-head'),
            onto=metadata.get('onto'),
        ) if remote_oid else bool(
            expected.before_head
            and metadata.get('head-name') == f'refs/heads/{expected.branch}'
            and metadata.get('orig-head') == expected.before_head
            and metadata.get('onto') == expected.target_base_oid
        )
        if not owned:
            return reject('rebase_target_or_original_head_mismatch')
        if observed.base_is_ancestor is None:
            return RebaseDecision(RebaseStatus.UNAVAILABLE, 'ancestry_missing')
        if observed.base_is_ancestor is not True:
            return reject('detached_head_outside_target_base')
        return RebaseDecision(RebaseStatus.WAITING, 'owned_rebase_in_progress')
    if (observed.branch != expected.branch
            or observed.branch_ref[0] != observed.head
            or f'branch=refs/heads/{expected.branch}' not in registration
            or observed.branch_worktrees != (expected.worktree,)
            or any(item.startswith('detached=') for item in registration)):
        return reject('final_branch_or_head_mismatch')
    if observed.base_is_ancestor is None:
        return RebaseDecision(RebaseStatus.UNAVAILABLE, 'ancestry_missing')
    if observed.base_is_ancestor is not True:
        if (not controlling_git_operation(operations)
                and observed.index_clean and not observed.staged
                and not any(entry.stage for entry in observed.index)):
            return RebaseDecision(RebaseStatus.START, 'owned_original_head_before_rebase')
        return reject('target_base_not_ancestor')
    if expected.autostash_oid and (
            any(entry.stage for entry in observed.index) or observed.staged):
        if set(operations) - {'MERGE_MSG', 'MERGE_MODE', 'REBASE_HEAD'}:
            return reject('unresolved_operation_or_index')
        return RebaseDecision(RebaseStatus.WAITING, 'owned_autostash_restore_pending')
    if (adopted_head == observed.head and observed.staged
            and not controlling_git_operation(operations)
            and not any(entry.stage for entry in observed.index)):
        # Already adopted checkpoints may carry preserved or new Worker work.
        # This is ownership for revalidation, never a completed rebase result.
        return RebaseDecision(RebaseStatus.WAITING, 'adopted_checkpoint_work_pending')
    if (controlling_git_operation(operations)
            or any(entry.stage for entry in observed.index)
            or observed.index_clean is not True or observed.staged):
        return reject('unresolved_operation_or_index')
    acceptance = {
        'kind': ACCEPTANCE_KIND, 'policy_version': POLICY_VERSION,
        'repo': expected.repo, 'repository_identity': expected.repository_identity,
        'plan_id': expected.plan_id, 'plan_path': expected.plan_path,
        'worktree': expected.worktree, 'branch': expected.branch,
        'target_base_oid': expected.target_base_oid,
        'remote': asdict(expected.remote), 'head': observed.head,
        'generation': expected.generation,
        'role': expected.role,
        'input_fingerprint': expected.input_fingerprint,
        'scope_fingerprint': expected.scope_fingerprint,
        'state_fingerprint': observed.fingerprint,
    }
    acceptance['id'] = fingerprint(acceptance)
    return RebaseDecision(RebaseStatus.COMPLETE, acceptance=acceptance)


def valid_acceptance(value: Mapping[str, object]) -> bool:
    """Validate a stored current-result record without treating it as a receipt."""
    fields = {
        'kind', 'policy_version', 'repo', 'repository_identity', 'plan_id',
        'plan_path', 'worktree', 'branch', 'target_base_oid', 'remote', 'head',
        'generation', 'input_fingerprint', 'scope_fingerprint', 'state_fingerprint',
        'id', 'role',
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        return False
    try:
        remote_value = dict(value['remote'])
        pr_value = remote_value.pop('pull_request')
        remote = Remote(**remote_value, pull_request=(
            PullRequest(**pr_value) if pr_value is not None else None
        ))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        value.get('kind') == ACCEPTANCE_KIND
        and value.get('policy_version') == POLICY_VERSION
        and type(value.get('policy_version')) is int
        and value.get('role') == 'worker'
        and type(value.get('generation')) is int and value['generation'] >= 0
        and all(isinstance(value.get(key), str) and value[key] for key in fields - {
            'policy_version', 'generation', 'remote',
        })
        and isinstance(value.get('remote'), dict)
        and _known_remote(remote, value['branch'])
        and value.get('id') == fingerprint({key: item for key, item in value.items()
                                            if key != 'id'})
    )
