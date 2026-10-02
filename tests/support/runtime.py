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
                     branch='loop-manager/G-047', worktree='/owned')
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
