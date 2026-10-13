from pathlib import Path
from tests.support.database import runtime_connection
import dataclasses
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.rebase_observations import decision_for, expectation, observation


def runtime_rebase_fixture(test, *, legacy=False):
    connection = runtime_connection()
    test.addCleanup(connection.close)
    runtime = runtime_state.RuntimeStateRepository(connection)
    runtime.activate('repo', 'G-047', activation_sha='old-base',
                     branch='loop-manager/G-047', worktree=str(Path('/owned')))
    runtime.reserve_workspace('repo', 'G-047', reserved_value=1, allowed_values=(1,))
    current = runtime.projected('repo')
    checkpoint = ExecutionCheckpointRepository(connection)
    if not legacy:
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base', observed_remote_oid='remote',
            snapshot={'old_journal': 'failed-audit', 'stage_evidence': {'tree': 'old'}},
        )
    expected = expectation(repo='repo', plan_id=current.plan_id, plan_path=current.path,
                           branch=current.branch, worktree=current.worktree,
                           generation=checkpoint.get('repo', 'G-047').generation,
                           remote=dataclasses.replace(expectation().remote, branch=current.branch))
    decision = decision_for(expected, observation(expected))
    return connection, runtime, checkpoint, current, expected, decision


def record_saved_scope_invocation(connection, repo, state_dir, worktree, role, before,
                                  session_id, *, plan_id=None):
    """Model a completed isolated Agent call, without injecting runtime allowances."""
    from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
    from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence, inspect_worktree
    from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
    from loop_manager_internal.agents import agent_scope_decision
    from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
    from loop_manager_internal.runtime.runtime_identity import _canonical_path

    ledger = InvocationRepository(connection)
    identity = ledger.start(InvocationContext(str(repo), plan_id), role, 'codex',
                            ResolvedProfile('test-fast', 'medium'), session_id,
                            scope_id=before.scope_id)
    project = RuntimeStateRepository(connection).scope_owner(str(repo))
    digest = agent_scope_decision.invocation_scope_digest(
        before, role=role, phase=agent_scope_decision.invocation_scope_phase(role),
        base_branch=str(project['base_branch']),
        branch=before.branch if role in {'worker', 'reviewer'} else None,
        worktree_key=str(_canonical_path(worktree)),
        default_worktree=str(_canonical_path(Path(repo))))
    ledger.record_isolation(identity, IsolationEvidence(
        'codex', 'codex fixture', 'Linux:fixture', role, str(worktree.resolve()),
        inspect_worktree(worktree, state_dir).repository_identity, 'a' * 64, 'verified',
        snapshot_digest=digest, configuration_digest='b' * 64))
    ledger.finish(identity, 'success', 0, 10, session_id, Usage())
    return identity
