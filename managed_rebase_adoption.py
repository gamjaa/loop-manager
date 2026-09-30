"""Observe and adopt owned current rebase results without replaying history.

Execution handlers and direct recovery use the same service. Acceptance changes
only the candidate baseline; Worker, verify, Review and publication remain
separate boundaries.
"""
from dataclasses import dataclass, replace
import hashlib

from managed_rebase_observation import ObservationAdapters, observe_current_rebase
from managed_rebase_validation import (
    ObservationFailure, RebaseExpectation, RebaseStatus, assess_current_rebase, valid_acceptance,
)
from manager_git_journal import MutationEvidenceError


def revalidation_values(target):
    return dict(
        phase='WORKING', active_sha=target, validated_base_sha=None,
        worker_result=None, worker_input_token=None, archive_token=None,
        verify_result=None, review_result=None, verify_token=None,
        review_token=None, publication_token=None, revision_resume_status=None,
        last_error=None,
    )


@dataclass(frozen=True)
class ManagedRebaseService:
    expected: RebaseExpectation
    adapters: ObservationAdapters

    def observe(self, *, checkpoint=None):
        observed, scope = observe_current_rebase(self.expected, self.adapters)
        accepted = (checkpoint.snapshot or {}).get('current_rebase_acceptance') if checkpoint else None
        adopted_head = None
        if (checkpoint is not None and checkpoint.operation != 'rebase'
                and valid_acceptance(accepted)
                and all(accepted[key] == getattr(self.expected, key) for key in (
                    'repo', 'repository_identity', 'plan_id', 'worktree', 'branch',
                    'target_base_oid', 'role'))):
            adopted_head = checkpoint.expected_head
        decision = assess_current_rebase(self.expected, observed, scope,
                                         adopted_head=adopted_head)
        return observed, decision

    def require_owned(self, *, checkpoint=None):
        observed, decision = self.observe(checkpoint=checkpoint)
        if decision.status not in {RebaseStatus.START, RebaseStatus.WAITING, RebaseStatus.COMPLETE}:
            detail = ': ' + observed.detail if isinstance(observed, ObservationFailure) else ''
            raise MutationEvidenceError('current rebase rejected: ' + decision.reason + detail)
        return observed, decision

    def adopt(self, state, checkpoint):
        observed, decision = self.require_owned()
        if decision.status is not RebaseStatus.COMPLETE:
            raise MutationEvidenceError('current rebase is still waiting for resolution')
        state.complete_rebase(
            checkpoint, acceptance=decision.acceptance,
            reobserve=lambda: self.observe()[1],
            project_values=revalidation_values(self.expected.target_base_oid),
        )
        return observed


def needs_agent_comparison(checkpoint, original, current):
    """Only absorb a rebase across a saved scope, not a fresh Agent's work."""
    accepted = (checkpoint.snapshot or {}).get('current_rebase_acceptance')
    return bool(checkpoint.operation == 'rebase' or (
        checkpoint.operation is None and isinstance(accepted, dict)
        and accepted.get('head') == current.head
        and original.head != current.head))


def selected_baseline(snapshot, current, branch, worktree):
    """Replace only accepted selected state, keeping other refs/config intact.

    Callers validate actual Agent output first and recheck current ownership.
    This is an in-memory comparison baseline, never an Agent execution receipt.
    """
    refs, worktrees = dict(snapshot.refs), dict(snapshot.worktrees)
    ref = f'refs/heads/{branch}'
    refs[ref] = current.refs[ref]
    worktrees[worktree] = current.worktrees[worktree]
    return replace(snapshot, head=current.head, branch=current.branch,
                   index=current.index, dirty=current.dirty,
                   operations=current.operations, refs=refs, worktrees=worktrees)


def remember_agent_baseline(checkpoints, repo, plan_id, original, effective, role):
    """Keep the observed start of a resumed Agent separate from its old snapshot."""
    current = checkpoints.get(repo, plan_id)
    baseline = {
        'kind': 'current_rebase_comparison_baseline', 'role': role,
        'original': hashlib.sha256(original.serialize().encode()).hexdigest(),
        'snapshot': effective.serialize(),
    }
    saved = dict(current.snapshot or {})
    if saved.get('current_rebase_agent_baseline') is not None:
        saved['current_rebase_agent_baseline_history'] = [
            *saved.get('current_rebase_agent_baseline_history', ()), saved['current_rebase_agent_baseline']]
    checkpoints.record(repo, plan_id, expected_generation=current.generation,
        operation=current.operation, expected_head=current.expected_head,
        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
        snapshot={**saved, 'current_rebase_agent_baseline': baseline})


def agent_comparison_baseline(original, saved, role):
    """Read a comparison start, never infer Agent execution from manual progress."""
    baseline = saved.get('current_rebase_agent_baseline')
    if baseline is None:
        return original
    if not isinstance(baseline, dict) or baseline.get('kind') != 'current_rebase_comparison_baseline':
        raise MutationEvidenceError('current rebase Agent comparison is malformed')
    if (baseline.get('role') != role
            or baseline.get('original') != hashlib.sha256(original.serialize().encode()).hexdigest()):
        # A later Agent scope has its own original start. Retain old comparison
        # records as audit instead of making them a historical gate.
        return original
    return type(original).deserialize(baseline['snapshot'])
