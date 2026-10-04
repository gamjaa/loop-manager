"""git direct rebase service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.execution.execution_state import ExecutionStatePort as _ExecutionStatePort
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.git import managed_rebase_publication
from loop_manager_internal.git import managed_rebase_runtime
from loop_manager_internal.git import managed_rebase_validation
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import json
import os
import re
import sqlite3
import subprocess


@dataclass(frozen=True)
class OwnedRebaseStateDirEffects:
    """Named effects for _owned_rebase_state_dir; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    expected_execplan_workspace: Callable[..., object]


def _owned_rebase_state_dir(effects: OwnedRebaseStateDirEffects, repo: str, plan_id: str, worktree: Path) -> Path | None:
    """Legacy recovery calls can be journaled only for a deterministic plan path."""
    if not re.fullmatch(r"G-\d+", plan_id) or len(worktree.parents) < 3:
        return None
    state_dir = worktree.parents[2]
    _branch, expected = effects.expected_execplan_workspace(state_dir, repo, plan_id)
    return state_dir if effects._canonical_path(expected) == effects._canonical_path(worktree) else None


@dataclass(frozen=True)
class OwnedRebaseMetadataEffects:
    """Named effects for _owned_rebase_metadata; no entry-point/module access."""
    git: Callable[..., object]


def _owned_rebase_metadata(effects: OwnedRebaseMetadataEffects, worktree: Path) -> dict[str, str] | None:
    for directory in ("rebase-merge", "rebase-apply"):
        values = {}
        for name in ("orig-head", "head-name", "onto"):
            result = effects.git(worktree, "rev-parse", "--git-path", f"{directory}/{name}",
                         check=False)
            if result.returncode:
                break
            path = Path(result.stdout.strip())
            if not path.is_absolute():
                path = worktree / path
            if not path.is_file():
                break
            values[name] = path.read_text(encoding="utf-8").strip()
        if len(values) == 3:
            return values
    return None


@dataclass(frozen=True)
class OwnedRebaseJournalStateEffects:
    """Named effects for _owned_rebase_journal_state; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _owned_rebase_metadata: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]


def _owned_rebase_journal_state(effects: OwnedRebaseJournalStateEffects, worktree: Path, branch: str) -> dict[str, object]:
    snapshot = effects.capture_git_snapshot(worktree)
    metadata = effects._owned_rebase_metadata(worktree)
    rebase_head = None
    if metadata is not None:
        result = effects.git(worktree, 'rev-parse', '--verify', 'REBASE_HEAD', check=False)
        if result.returncode == 0:
            rebase_head = result.stdout.strip()
    return json.loads(json.dumps({
        "head": snapshot.head, "branch": snapshot.branch,
        "ref": snapshot.refs.get(f"refs/heads/{branch}"),
        "index": snapshot.index, "dirty": snapshot.dirty,
        "file_evidence": rebase_file_evidence.capture(worktree, snapshot.head, snapshot.dirty),
        "registration": snapshot.worktrees.get(str(effects._canonical_path(worktree))),
        "operations": snapshot.operations,
        "metadata": metadata, "rebase_head": rebase_head,
        "unmerged": bool(effects.git(worktree, "ls-files", "-u", "-z").stdout),
        "index_clean": effects.git(worktree, "diff", "--cached", "--quiet",
                           check=False).returncode == 0,
    }, sort_keys=True))


@dataclass(frozen=True)
class CurrentRebaseServiceEffects:
    """Named effects for _current_rebase_service; no entry-point/module access."""
    _managed_rebase_runtime_effects: Callable[..., object]


def _current_rebase_service(effects: CurrentRebaseServiceEffects, connection, repo_path, state_dir, repo, plan_id,
                            worktree, branch, **options):
    return managed_rebase_runtime.build_service(
        effects._managed_rebase_runtime_effects(), connection, repo_path, state_dir, repo, plan_id,
        worktree, branch, **options,
    )


@dataclass(frozen=True)
class FinishOwnedDirectRebaseEffects:
    """Named effects for _finish_owned_direct_rebase; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]


def _finish_owned_direct_rebase(
    effects: FinishOwnedDirectRebaseEffects, state_dir, connection, repo_path, repo, plan_id, worktree, branch,
    *, allow_equal=False, agent_session_id=None,
):
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
    if checkpoint.operation != 'rebase':
        return False
    service = effects._current_rebase_service(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch,
    )
    _, decision = service.require_owned()
    if decision.status.value == 'ready_to_start':
        return False
    return manager_git_journal.record_current_rebase_result(
        state_dir, repo=str(effects._canonical_path(repo_path)),
        target=str(effects._canonical_path(worktree)), owner=f'plan:{plan_id}',
        decision=decision, observed=effects._owned_rebase_journal_state(worktree, branch),
    )


@dataclass(frozen=True)
class JournaledDirectRebaseEffects:
    """Named effects for _journaled_direct_rebase; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _finish_owned_direct_rebase: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]
    _owned_rebase_metadata: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    git: Callable[..., object]
    remote_branch_oid: Callable[..., object]


def _journaled_direct_rebase(
    effects: JournaledDirectRebaseEffects, connection: sqlite3.Connection, repo_path: Path, repo: str, plan_id: str,
    worktree: Path, branch: str, target_oid: str, *, autostash: bool = False,
    agent_session_id: str | None = None,
    observed_remote: tuple[str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        raise manager_git_journal.MutationEvidenceError(
            'direct rebase: owned journal workspace unavailable'
        )
    before = effects._owned_rebase_journal_state(worktree, branch)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    remote_oid = (effects.remote_branch_oid(repo_path, branch) if observed_remote is None
                  else observed_remote[0])
    if current.observed_remote_oid not in (None, remote_oid):
        raise manager_git_journal.MutationEvidenceError(
            "Manager rebase 전 게시 원격 OID가 checkpoint와 다릅니다."
        )
    if manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch) != remote_oid:
        raise manager_git_journal.MutationEvidenceError(
            'Manager rebase start: live remote differs from fetched OID'
        )
    if current.operation == 'rebase':
        service = effects._current_rebase_service(connection, repo_path, state_dir, repo,
                                         plan_id, worktree, branch)
        _, decision = service.require_owned()
        if decision.status.value == 'complete':
            effects._complete_direct_rebase_checkpoint(
                connection, repo, plan_id, worktree, current.target_base_oid,
                repo_path=repo_path, branch=branch, agent_session_id=agent_session_id)
            return subprocess.CompletedProcess([], 0, 'current rebase already complete')
        if decision.status.value != 'ready_to_start':
            return subprocess.CompletedProcess([], 1, 'owned rebase is waiting for resolution')
        if current.target_base_oid != target_oid:
            raise manager_git_journal.MutationEvidenceError('current rebase target differs')
    else:
        try:
            row = connection.execute(
                'SELECT pr_head_oid, pr_conflict_head_oid FROM projects WHERE repo = ?',
                (repo,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        pr_head_oid = (str(row['pr_head_oid'] or row['pr_conflict_head_oid'] or '')
                       or None) if row is not None else None
        if pr_head_oid and pr_head_oid != remote_oid:
            raise manager_git_journal.MutationEvidenceError(
                'Manager rebase start: PR head differs from remote OID'
            )
        checkpoint.record(
            repo, plan_id, expected_generation=current.generation,
            operation='rebase', expected_head=before['head'],
            target_base_oid=target_oid, observed_remote_oid=remote_oid,
            snapshot={**(current.snapshot or {}), 'before_head': before['head'],
                      'rebase_remote_oid': remote_oid, 'rebase_policy': 'current_state'},
        )
    token = manager_git_journal.begin_current_rebase(
        state_dir, owner=f'plan:{plan_id}', repo=str(effects._canonical_path(repo_path)),
        target=str(effects._canonical_path(worktree)), before=before,
        expected={'branch': branch, 'before_head': before['head'],
                  'target_base_oid': target_oid, 'remote_oid': remote_oid,
                  'agent_session_id': agent_session_id})
    environment = dict(os.environ)
    environment["GIT_REFLOG_ACTION"] = f"loop-manager-rebase:{token}"
    scoped_options = (("-c", f"core.hooksPath={os.devnull}", "-c", "rebase.updateRefs=false")
                      if observed_remote is not None else ())
    result = effects.git(worktree, *scoped_options, "rebase", *(("--autostash",) if autostash else ()),
                 target_oid, check=False, env=environment)
    if result.returncode == 0:
        manager_git_journal.record_rebase_invocation(
            state_dir, str(effects._canonical_path(repo_path)),
            str(effects._canonical_path(worktree)), token, returncode=result.returncode,
        )
    if effects.git(worktree, 'ls-files', '-u', '-z').stdout and not effects._owned_rebase_metadata(worktree):
        matched = re.search(r'Created autostash: ([0-9a-fA-F]{7,64})', result.stdout)
        if matched:
            stash = effects.git(worktree, 'rev-parse', '--verify', f'{matched.group(1)}^{{commit}}', check=False)
            if stash.returncode == 0:
                current = checkpoint.get(repo, plan_id)
                checkpoint.record(repo, plan_id, expected_generation=current.generation,
                    operation='rebase', expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'stash_oid': stash.stdout.strip()})
    effects._finish_owned_direct_rebase(
        state_dir, connection, repo_path, repo, plan_id, worktree, branch,
        allow_equal=True, agent_session_id=agent_session_id,
    )
    return result


@dataclass(frozen=True)
class DirectRebaseHistoryProvenEffects:
    """Named effects for _direct_rebase_history_proven; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]


def _direct_rebase_history_proven(
    effects: DirectRebaseHistoryProvenEffects, connection, repo_path, repo, plan_id, worktree, branch, before_head,
    after_head, target_base, agent_session_id,
):
    # Compatibility name: assess the current result, not history.
    state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        return False
    try:
        observed, decision = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            target=target_base,
        ).observe()
        if decision.status is managed_rebase_validation.RebaseStatus.UNAVAILABLE:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'direct rebase observation unavailable')
        return decision.status.value == 'complete' and observed.head == after_head
    except RecoveryFailure:
        raise
    except manager_git_journal.MutationEvidenceError:
        return False
    except (RuntimeError, OSError) as error:
        raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                              'direct rebase observation failed') from error


def _direct_rebase_result_matches_observation(
    sealed: Mapping[str, object], observed: Mapping[str, object],
    *, preserved_worktree: bool,
) -> bool:
    if sealed == observed:
        return True
    return bool(
        preserved_worktree and sealed.get('metadata') is None
        and observed.get('metadata') is None
        and all(sealed.get(field) == observed.get(field) for field in (
            'head', 'branch', 'ref', 'registration', 'operations',
        ))
    )


@dataclass(frozen=True)
class DirectRebaseRestoredStageMatchesEffects:
    """Named effects for _direct_rebase_restored_stage_matches; no entry-point/module access."""
    _direct_rebase_result_matches_observation: Callable[..., object]


def _direct_rebase_restored_stage_matches(
    effects: DirectRebaseRestoredStageMatchesEffects, intent: Mapping[str, object], sealed: Mapping[str, object],
    observed: Mapping[str, object],
) -> bool:
    """Prove Manager-restored staging from the rebase's frozen before state."""
    before = intent.get('before')
    if (not isinstance(before, dict)
            or not isinstance(before.get('dirty'), dict)
            or not before['dirty']
            or not isinstance(before.get('index'), dict)
            or not isinstance(sealed.get('dirty'), dict)
            or not isinstance(sealed.get('index'), dict)
            or not isinstance(observed.get('dirty'), dict)
            or not isinstance(observed.get('index'), dict)
            or set(sealed['dirty']) != set(before['dirty'])
            or observed['dirty'] != before['dirty']
            or observed.get('file_evidence') != sealed.get('file_evidence')
            or observed.get('rebase_head') != sealed.get('rebase_head')
            or observed.get('unmerged') != sealed.get('unmerged')
            or not effects._direct_rebase_result_matches_observation(
                sealed, observed, preserved_worktree=True,
            )):
        return False
    staged_paths = {
        path for path, state in before['dirty'].items()
        if (isinstance(state, list) and state
            and isinstance(state[0], list) and 'staged' in state[0])
    }
    if not staged_paths or observed.get('index_clean') is not False:
        return False
    for path in before['index'].keys() | sealed['index'].keys() | observed['index'].keys():
        expected = (before['index'] if path in staged_paths else sealed['index'])
        if observed['index'].get(path) != expected.get(path):
            return False
    return True


@dataclass(frozen=True)
class CompleteDirectRebaseCheckpointEffects:
    """Named effects for _complete_direct_rebase_checkpoint; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _publish_direct_rebase_checkpoint: Callable[..., object]


def _complete_direct_rebase_checkpoint(
    effects: CompleteDirectRebaseCheckpointEffects, connection, repo, plan_id, worktree, target_base_oid, *, repo_path,
    branch, agent_session_id, preserved_worktree=False, preserved_marker=None,
    preserved_observation=None,
):
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    saved = current.snapshot or {}
    legacy_completed = (current.operation is None and not saved.get('current_rebase_acceptance')
                        and (saved.get('completed_rebase') or saved.get('rebase_completion')))
    publication_pending = (current.operation == 'rebase_publish'
                           or current.operation is None
                           and managed_rebase_validation.valid_acceptance(saved.get('current_rebase_acceptance')))
    if current.operation != 'rebase' and not legacy_completed and not publication_pending:
        return
    state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None or current.target_base_oid != target_base_oid:
        raise manager_git_journal.MutationEvidenceError('current rebase target or workspace missing')
    if publication_pending:
        effects._publish_direct_rebase_checkpoint(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch)
        return
    if preserved_observation is not None:
        live = effects._owned_rebase_journal_state(worktree, branch)
        if live != preserved_observation:
            changed_fields = sorted(key for key in live.keys() | preserved_observation.keys()
                                    if live.get(key) != preserved_observation.get(key))
            raise manager_git_journal.MutationEvidenceError(
                f'direct rebase completion: observation drift={changed_fields}')
    service = effects._current_rebase_service(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch,
    )
    runtime = runtime_state.RuntimeStateRepository(connection)
    projected = runtime.projected(repo)
    if projected is None or projected.status == 'PLAN_BLOCKED':
        selected = runtime.get(repo, plan_id)
        observed, decision = service.require_owned()
        if selected is None or selected.status != 'PLAN_BLOCKED' or decision.status.value != 'complete':
            raise manager_git_journal.MutationEvidenceError('current rebase execution is not selected')
        if projected is not None:
            runtime.complete_rebase(
                selected, checkpoint, acceptance=decision.acceptance,
                project_values={'phase': selected.phase},
                reobserve=lambda: service.observe()[1],
            )
        else:
            with runtime_state._transaction(connection):
                if runtime.get(repo, plan_id) != selected or checkpoint.get(repo, plan_id) != current:
                    raise manager_git_journal.MutationEvidenceError('current blocked rebase ownership changed')
                if service.observe()[1] != decision or runtime.projected(repo) is not None:
                    raise manager_git_journal.MutationEvidenceError('current blocked rebase state changed')
                values = managed_rebase_adoption.revalidation_values(target_base_oid)
                for field in ('phase', 'last_error', 'revision_resume_status'):
                    values.pop(field)
                checkpoint.accept_current_rebase(
                    repo, plan_id, acceptance=decision.acceptance,
                    prior_artifacts={field: getattr(selected, field) for field in values},
                )
                runtime.transition(repo, plan_id, plan_values=values,
                                   project_values={'phase': connection.execute(
                                       'SELECT phase FROM projects WHERE repo = ?', (repo,)
                                   ).fetchone()[0]},
                                   expected_status='PLAN_BLOCKED', enqueue=False)
    else:
        if projected.plan_id != plan_id:
            raise manager_git_journal.MutationEvidenceError('current rebase execution is not selected')
        service.adopt(_ExecutionStatePort(connection, runtime, repo), checkpoint)
    # Preserve the clean-index publication boundary before a blocked release
    # restores its original staging for the next Worker turn.
    effects._publish_direct_rebase_checkpoint(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch)


@dataclass(frozen=True)
class PublishDirectRebaseCheckpointEffects:
    """Named effects for _publish_direct_rebase_checkpoint; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]


def _publish_direct_rebase_checkpoint(effects: PublishDirectRebaseCheckpointEffects, connection, repo_path, state_dir, repo,
                                      plan_id, worktree, branch):
    from loop_manager_internal.execution.execution_state import DirectRebasePublicationStatePort
    project_recovery = scoped_blocking.project_block(connection, repo) is not None
    if project_recovery and not project_blocked_recovery.project_slot_available(connection, repo, plan_id):
        raise manager_git_journal.MutationEvidenceError('project checkpoint publication is not selected')

    def publication_git(target, *arguments, **options):
        if project_recovery and arguments[:1] == ('push',):
            return effects.git(target, '-c', f'core.hooksPath={os.devnull}', *arguments, **options)
        return effects.git(target, *arguments, **options)

    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    state = DirectRebasePublicationStatePort(
        connection, runtime_state.RuntimeStateRepository(connection), repo, plan_id)
    owner = connection.execute(
        "SELECT accepted_remote_oid FROM blocked_checkpoints WHERE repo = ? AND plan_id = ? "
        "AND phase = 'published'", (repo, plan_id)).fetchone()

    def owns_remote(oid, selected):
        if owner is None:
            return True
        original = owner['accepted_remote_oid']
        if not original:
            return False
        return checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, plan_id, branch, worktree, original, effects.git,
            recorded_oid=original, require_live=oid == original,
            strict_observation=True) == original

    managed_rebase_publication.publish_checkpoint(
        state=state, checkpoints=checkpoints, repo=repo, plan_id=plan_id,
        repo_path=repo_path, worktree=worktree, branch=branch,
        service=lambda **options: effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch, **options),
        owns_remote=owns_remote, git=publication_git, lookup_pr=effects.pr_for_branch)
