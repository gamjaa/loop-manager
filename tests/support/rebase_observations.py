from loop_manager import _worker_path_allowed
from loop_manager_internal.git.managed_rebase_validation import (
    FileChange,
    IndexEntry,
    RebaseExpectation,
    RebaseObservation,
    Remote,
    assess_current_rebase,
    assess_scope,
)


def expectation(**changes):
    values = dict(
        repo='/repo', repository_identity='/repo/.git', plan_id='G-108',
        plan_path='docs/plans/[G-108] Current-State Managed Rebase Validation.md',
        worktree='/owned', branch='loop-manager/G-108', target_base_oid='base',
        generation=1, workspace_owned=True, input_fingerprint='inputs',
        scope_fingerprint='worker-contract',
        remote=Remote('owner/repo', 'loop-manager/G-108', 'remote'),
        before_head='before',
    )
    values.update(changes)
    return RebaseExpectation(**values)

def observation(expected=None, **changes):
    expected = expected or expectation()
    values = dict(
        repo=expected.repo, repository_identity=expected.repository_identity,
        worktree=expected.worktree, identity_verified=True, head='after',
        branch=expected.branch, branch_ref=('after', ''),
        registration=('HEAD=after', f'branch=refs/heads/{expected.branch}'),
        branch_worktrees=(expected.worktree,),
        operations=(), metadata=None, base_is_ancestor=True, index_clean=True,
        index=(IndexEntry('app.py', '100644', 'blob', 0, 'H', 0),),
        committed=(FileChange('app.py', '100644', '100644', 'old', 'blob', 'M'),),
        staged=(), files=(), remote=expected.remote, input_fingerprint='inputs',
        scope_fingerprint='worker-contract', repository_state_fingerprint='repository',
    )
    values.update(changes)
    return RebaseObservation(**values)

def scope_for(expected, observed, **changes):
    arguments = dict(
        path_allowed=lambda path: _worker_path_allowed(path, expected.plan_path),
        contracts_allowed=lambda _observed: True,
    )
    arguments.update(changes)
    return assess_scope(expected, observed, **arguments)

def decision_for(expected=None, observed=None):
    expected = expected or expectation()
    observed = observed or observation(expected)
    return assess_current_rebase(expected, observed, scope_for(expected, observed))
