"""Publish an owned, completed rebase before any later candidate work.

The intent is independent of final PR publication. Current-state validation owns
Git acceptance; this boundary owns the exact remote lease and resumable push.
"""
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus, valid_acceptance
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery.blocked_checkpoint import checkpoint_pr_matches
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
import sqlite3


class RebasePublicationRejected(RecoveryFailure):
    def __init__(self, detail):
        super().__init__(RecoveryReason.REMOTE_OID_UNCERTAIN, detail)


class RebaseObservationRetry(RecoveryFailure):
    """An external read failed; a saved publication intent can be retried."""

    def __init__(self, detail):
        super().__init__(RecoveryReason.PUBLICATION_RETRY, detail)


def publish_checkpoint(*, state, checkpoints, repo, plan_id, repo_path, worktree,
                       branch, service, owns_remote, git, lookup_pr, candidate_head=None):
    try:
        current = checkpoints.get(repo, plan_id)
        saved = current.snapshot or {}
        if saved.get('pending_work_branch_input'):
            return
        intent = saved.get('rebase_publication_intent')
        pending = current.operation == 'rebase_publish'
        if pending and current.rebase_publication_intent() is None:
            raise RebasePublicationRejected('intermediate checkpoint intent identity differs')
        if pending and candidate_head is not None and intent['head'] != candidate_head:
            raise RebasePublicationRejected('Worker checkpoint differs from the pending rebase publication')
        acceptance = saved.get('current_rebase_acceptance')
        if not pending and (current.operation is not None or not valid_acceptance(acceptance)):
            return
        if candidate_head is not None and not pending:
            if (current.expected_head != candidate_head
                    or git(worktree, 'merge-base', '--is-ancestor', acceptance['head'],
                           candidate_head, check=False).returncode != 0):
                raise RebasePublicationRejected('Worker checkpoint no longer follows the accepted rebase')
            try:
                _, decision = service().observe(checkpoint=current)
            except (RecoveryFailure, manager_git_journal.MutationEvidenceError):
                raise
            except (RuntimeError, OSError) as error:
                raise RebaseObservationRetry('Worker checkpoint rebase observation failed') from error
            if decision.status is RebaseStatus.UNAVAILABLE:
                raise RebaseObservationRetry('Worker checkpoint rebase observation unavailable: ' + decision.reason)
            fresh = decision.acceptance
            if (decision.status is not RebaseStatus.COMPLETE or not valid_acceptance(fresh)
                    or fresh['head'] != candidate_head
                    or any(fresh[key] != acceptance[key] for key in (
                        'repo', 'plan_id', 'repository_identity', 'worktree', 'branch', 'target_base_oid'))
                    or any(fresh['remote'][key] != acceptance['remote'][key]
                           for key in ('repository', 'branch'))
                    or (fresh['remote']['pull_request'] or {}).get('identity')
                       != (acceptance['remote']['pull_request'] or {}).get('identity')):
                raise RebasePublicationRejected('Worker checkpoint current rebase ownership or scope differs')
            acceptance = fresh
        row = state.row()
        if not pending and not acceptance['remote']['oid']:
            return
        if (not pending and candidate_head is None
                and saved.get('rebase_publication_result', {}).get('acceptance_id') == acceptance['id']):
            return
        if not pending and candidate_head is None and saved.get('rebase_publication_deferred') == acceptance['id']:
            return
        owner = checkpoints.connection.execute(
            "SELECT accepted_remote_oid FROM blocked_checkpoints WHERE repo = ? AND plan_id = ? "
            "AND phase = 'published' AND branch = ? AND worktree = ?",
            (repo, plan_id, branch, str(worktree))).fetchone()
    except sqlite3.OperationalError as error:
        raise RebaseObservationRetry('intermediate checkpoint database observation failed') from error
    expected_oid = intent['remote_before'] if pending else acceptance['remote']['oid']
    if not pending:
        known_remote_oids = {value for value in (
            saved.get('accepted_work_branch'), row['publication_remote_oid'],
            row['pr_head_oid'] if row['pr_url'] else None, owner[0] if owner else None,
        ) if value}
        if expected_oid not in known_remote_oids:
            raise RebasePublicationRejected('current rebase remote has no durable publication identity')
    if not expected_oid:
        raise RebasePublicationRejected('intermediate checkpoint has no adopted remote OID')
    try:
        live_oid = manager_git_journal.live_remote_branch_oid(git, repo_path, branch)
    except (manager_git_journal.MutationEvidenceError, RuntimeError, OSError) as error:
        raise RebaseObservationRetry('intermediate checkpoint remote observation failed') from error
    if live_oid is not None and live_oid not in ({expected_oid, intent['head']} if pending else {expected_oid}):
        # Retire the old exact lease; current input reconciliation decides a new
        # candidate. This records no success for an unobserved push.
        checkpoints.record(repo, plan_id, expected_generation=current.generation,
            operation=None, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**saved, 'rebase_publication_intent': None,
                'rebase_publication_deferred': (intent['acceptance'] if pending else acceptance)['id'],
                'retired_rebase_publications': [*saved.get('retired_rebase_publications', ()),
                    {'intent': intent, 'observed_remote': live_oid, 'confirmed': False}]})
        return

    def inspect():
        try:
            state.check()
            selected = checkpoints.get(repo, plan_id)
        except sqlite3.OperationalError as error:
            raise RebaseObservationRetry('intermediate checkpoint database observation failed') from error
        # Use the actual remote only for observing a completed intent. Never
        # replace its expected lease with an unexpected remote observation.
        try:
            oid = manager_git_journal.live_remote_branch_oid(git, repo_path, branch)
        except manager_git_journal.MutationEvidenceError as error:
            raise RebaseObservationRetry(str(error)) from error
        head = intent['head'] if pending else acceptance['head']
        if oid not in ({expected_oid, head} if pending else {expected_oid}):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'intermediate checkpoint remote lease changed or disappeared')
        accepted_pr = (intent['acceptance'] if pending else acceptance)['remote']['pull_request']
        pr_url = accepted_pr['identity'] if accepted_pr else None
        if (row['pr_url'] or None) != pr_url:
            raise RebasePublicationRejected('intermediate checkpoint saved PR identity changed')
        try:
            live_pr = lookup_pr(repo_path, branch, pr_url)
        except (RuntimeError, OSError) as error:
            raise RebaseObservationRetry('intermediate checkpoint PR observation failed') from error
        if live_pr and live_pr.get('url') == pr_url and live_pr.get('headRefName') == branch and live_pr.get('headRefOid') != oid:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'intermediate checkpoint PR head observation changed')
        if not checkpoint_pr_matches(live_pr, url=pr_url, branch=branch, head=oid):
            raise RebasePublicationRejected('intermediate checkpoint PR identity or head changed')
        try:
            live = service(remote_oid=oid)
            observed, decision = live.observe(checkpoint=selected)
        except (RecoveryFailure, manager_git_journal.MutationEvidenceError):
            raise
        except (RuntimeError, OSError) as error:
            raise RebaseObservationRetry('intermediate checkpoint rebase observation failed') from error
        if decision.status is RebaseStatus.UNAVAILABLE:
            raise RebaseObservationRetry('intermediate checkpoint observation unavailable: ' + decision.reason)
        accepted = decision.acceptance
        if (decision.status is not RebaseStatus.COMPLETE or not valid_acceptance(accepted)
                or accepted['head'] != head
                or any(accepted[key] != (intent if pending else acceptance)[key]
                       for key in ('repo', 'plan_id', 'repository_identity', 'worktree',
                                   'branch', 'target_base_oid'))
                or (accepted['remote']['pull_request'] or {}).get('identity') != pr_url):
            raise RebasePublicationRejected('intermediate checkpoint current ownership or scope differs')
        try:
            owned = owns_remote(oid, selected,
                                rebase_observation=(live.expected, observed, decision))
        except RecoveryFailure as error:
            if error.reason is RecoveryReason.PUBLICATION_RETRY:
                raise RebaseObservationRetry('intermediate checkpoint ownership observation failed') from error
            raise
        if not owned:
            raise RebasePublicationRejected('intermediate checkpoint current ownership or scope differs')
        return oid, accepted

    try:
        oid, checked = inspect()
        if not pending:
            current = checkpoints.begin_rebase_publication(
                repo, plan_id, expected_generation=current.generation,
                acceptance=checked, remote_before=expected_oid)
            intent = current.snapshot['rebase_publication_intent']
            pending = True
        # Recheck exact live ref, PR identity and current ownership after the
        # durable intent, immediately before the external effect.
        oid, checked = inspect()
        if oid != intent['head']:
            pushed = git(worktree, 'push',
                         manager_git_journal.exact_branch_lease(branch, intent['remote_before']),
                         'origin', f"{intent['head']}:refs/heads/{branch}", check=False)
            if pushed.returncode:
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'intermediate checkpoint push failed: ' + pushed.stdout[-8000:])
        oid, checked = inspect()
        if oid != intent['head']:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'intermediate checkpoint push is not confirmed')
        state.complete_rebase_publication(checkpoints, intent=intent,
                                         acceptance=checked, reobserve=inspect)
    except RecoveryFailure:
        raise
    except (RuntimeError, OSError) as error:
        # A current-state mismatch is a refusal; transport errors can retry the
        # same intent. No failure clears intent, adopts a new lease or work.
        reason = (RecoveryReason.REMOTE_OID_UNCERTAIN
                  if isinstance(error, manager_git_journal.MutationEvidenceError)
                  else RecoveryReason.PUBLICATION_RETRY)
        raise RecoveryFailure(reason, str(error)) from error
