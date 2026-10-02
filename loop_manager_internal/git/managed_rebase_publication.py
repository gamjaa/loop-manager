"""Publish an owned, completed rebase before any later candidate work.

The intent is independent of final PR publication. Current-state validation owns
Git acceptance; this boundary owns the exact remote lease and resumable push.
"""
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus, valid_acceptance
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason


class RebasePublicationRejected(RecoveryFailure):
    def __init__(self, detail):
        super().__init__(RecoveryReason.REMOTE_OID_UNCERTAIN, detail)


def publish_checkpoint(*, state, checkpoints, repo, plan_id, repo_path, worktree,
                       branch, service, owns_remote, git, lookup_pr):
    current = checkpoints.get(repo, plan_id)
    saved = current.snapshot or {}
    intent = saved.get('rebase_publication_intent')
    pending = current.operation == 'rebase_publish'
    if pending and current.rebase_publication_intent() is None:
        raise RebasePublicationRejected('intermediate checkpoint intent identity differs')
    acceptance = saved.get('current_rebase_acceptance')
    if not pending and (current.operation is not None or not valid_acceptance(acceptance)):
        return
    row = state.row()
    if not pending and (row['pr_url'] or row['pr_conflict_recovery']
                        or acceptance['remote']['pull_request'] is not None
                        or not acceptance['remote']['oid']):
        return
    if not pending and saved.get('rebase_publication_result', {}).get('acceptance_id') == acceptance['id']:
        return
    owner = checkpoints.connection.execute(
        "SELECT accepted_remote_oid FROM blocked_checkpoints WHERE repo = ? AND plan_id = ? "
        "AND phase = 'published' AND branch = ? AND worktree = ?",
        (repo, plan_id, branch, str(worktree))).fetchone()
    expected_oid = (intent['remote_before'] if pending else
                    row['publication_remote_oid'] or (owner[0] if owner else None))
    if not pending and expected_oid is None:
        raise RebasePublicationRejected('existing remote branch has no durable publication identity')
    if not expected_oid:
        raise RebasePublicationRejected('intermediate checkpoint has no adopted remote OID')

    def inspect():
        state.check()
        selected = checkpoints.get(repo, plan_id)
        # Use the actual remote only for observing a completed intent. Never
        # replace its expected lease with an unexpected remote observation.
        oid = manager_git_journal.live_remote_branch_oid(git, repo_path, branch)
        head = intent['head'] if pending else acceptance['head']
        if oid not in ({expected_oid, head} if pending else {expected_oid}):
            raise RebasePublicationRejected('intermediate checkpoint remote lease changed or disappeared')
        if (row['pr_url'] or row['pr_conflict_recovery']
                or lookup_pr(repo_path, branch, None) is not None):
            raise RebasePublicationRejected('intermediate checkpoint branch now has a PR')
        live = service(remote_oid=oid)
        observed, decision = live.observe(checkpoint=selected)
        if decision.status is RebaseStatus.UNAVAILABLE:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'intermediate checkpoint observation unavailable: ' + decision.reason)
        accepted = decision.acceptance
        if (decision.status is not RebaseStatus.COMPLETE or not valid_acceptance(accepted)
                or accepted['head'] != head
                or any(accepted[key] != (intent if pending else acceptance)[key]
                       for key in ('repo', 'plan_id', 'repository_identity', 'worktree',
                                   'branch', 'target_base_oid'))
                or accepted['remote']['pull_request'] is not None
                or not owns_remote(oid, selected)):
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
        # Recheck exact live ref, PR absence and current ownership after the
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
        reason = (RecoveryReason.REMOTE_OID_UNCERTAIN if isinstance(error, manager_git_journal.MutationEvidenceError)
                  and str(error) != 'rebase remote: live branch query failed'
                  else RecoveryReason.PUBLICATION_RETRY)
        raise RecoveryFailure(reason, str(error)) from error
