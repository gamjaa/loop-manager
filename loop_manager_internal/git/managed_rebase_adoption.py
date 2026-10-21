"""Observe and adopt owned current rebase results without replaying history.

Execution handlers and direct recovery use the same service. Acceptance changes
only the candidate baseline; Worker, verify, Review and publication remain
separate boundaries.
"""
from dataclasses import dataclass, replace
from collections.abc import Callable

from loop_manager_internal.git.managed_rebase_observation import ObservationAdapters, observe_current_rebase
from loop_manager_internal.git.managed_rebase_validation import (
    ObservationFailure, RebaseExpectation, RebaseStatus, assess_current_rebase, valid_acceptance,
)
from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.verification.timing import phase


def checkpoint_remote_oid(checkpoint, repo, branch, *, git, read_fetched):
    """Keep current rebase observation on the live ref through publication."""
    if checkpoint is not None and (
            checkpoint.operation == 'rebase'
            or (checkpoint.snapshot or {}).get('accepted_work_branch')
            or valid_acceptance((checkpoint.snapshot or {}).get('current_rebase_acceptance'))):
        return manager_git_journal.live_remote_branch_oid(git, repo, branch)
    return read_fetched(repo, branch)


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
    normalizer: Callable[[], bool] | None = None
    declaration_error: RecoveryFailure | None = None

    def normalize_frontmatter(self):
        """Explicit Manager write after Git completes; observations remain read-only."""
        return self.normalizer() if self.normalizer is not None else False

    def observe(self, *, checkpoint=None):
        if self.declaration_error is not None:
            raise self.declaration_error
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
            if decision.reason == 'current_scope_mismatch':
                raise RecoveryFailure(RecoveryReason.AGENT_SCOPE_VIOLATION,
                    'current rebase rejected: ' + decision.reason)
            raise MutationEvidenceError('current rebase rejected: ' + decision.reason + detail)
        return observed, decision

    def adopt(self, state, checkpoint, *, validate_ownership=None):
        with phase('rebase:adoption_inspect'):
            observed, decision = self.require_owned()
        if decision.status is not RebaseStatus.COMPLETE:
            raise MutationEvidenceError('current rebase is still waiting for resolution')
        # The caller may consume this inspection for its separate durable-owner
        # check. It must be read-only; transaction confirmation below observes
        # again, and no inspection survives adoption or publication.
        if validate_ownership is not None:
            with phase('rebase:adoption_owner'):
                validate_ownership(observed, decision)

        def confirm():
            with phase('rebase:adoption_confirm'):
                return self.observe()[1]

        state.complete_rebase(
            checkpoint, acceptance=decision.acceptance,
            reobserve=confirm,
            project_values=revalidation_values(self.expected.target_base_oid),
        )
        return observed


def selected_baseline(snapshot, current, branch, worktree, *, publication=None):
    """Replace only accepted selected state, keeping other refs/config intact.

    Callers validate actual Agent output first and recheck current ownership.
    This is an in-memory comparison baseline, never an Agent execution receipt.
    """
    refs, worktrees = dict(snapshot.refs), dict(snapshot.worktrees)
    ref = f'refs/heads/{branch}'
    refs[ref] = current.refs[ref]
    if publication is not None:
        from loop_manager_internal.recovery.work_branch_integration import preserved_refs
        repo = (publication.snapshot or {}).get('integration_repo')
        if repo:
            for name in preserved_refs(repo, publication.snapshot or {}, current.refs):
                refs[name] = current.refs[name]
        intent = publication.rebase_publication_intent()
        result = (publication.snapshot or {}).get('rebase_publication_result') or {}
        pending = publication.operation == 'rebase_publish'
        confirmed = (intent is not None and result.get('intent_id') == intent['id']
                     and result.get('head') == intent['head']
                     and publication.observed_remote_oid == intent['head'])
        if (intent is not None and (pending or confirmed)
                and (intent['branch'], intent['worktree']) == (branch, worktree)):
            # A pending push may have changed its tracking ref before SQLite
            # completion. The caller rechecks the exact live lease and current
            # ownership; unrelated shared refs retain the original baseline.
            remote_ref = f'refs/remotes/origin/{branch}'
            allowed = {(intent['head'], '')}
            if pending:
                allowed.add((intent['remote_before'], ''))
            if current.refs.get(remote_ref) not in allowed:
                raise MutationEvidenceError('published comparison remote ref changed')
            refs[remote_ref] = current.refs[remote_ref]
    worktrees[worktree] = current.worktrees[worktree]
    return replace(snapshot, head=current.head, branch=current.branch,
                   index=current.index, dirty=current.dirty,
                   operations=current.operations, refs=refs, worktrees=worktrees)
