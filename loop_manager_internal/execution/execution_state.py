"""Snapshot-bound adapter for execution persistence and legacy policy helpers."""

from __future__ import annotations

from dataclasses import replace
from functools import wraps
from types import MappingProxyType

from loop_manager_internal.storage import runtime_state


class ExecutionStatePort:
    def __init__(self, connection, repository, repo):
        self.connection = connection
        self.repository = repository
        self.repo = repo
        self.expected = repository.projected(repo)
        if self.expected is None:
            raise runtime_state.RuntimeStateError("활성 execution snapshot이 없습니다.")
        # Terminal blocking can clear the projection, but not the selected identity.
        self.plan_id = self.expected.plan_id

    def check(self):
        current = self.repository.projected(self.repo)
        if current != self.expected:
            # Catalog refresh can change definition metadata while the
            # selected execution remains the same.  The revision guard owns
            # that refresh; only checkpoint correction pins the catalog
            # generation across its external observation.
            if current is None or replace(
                current,
                catalog_generation=self.expected.catalog_generation,
                current_revision_sha=self.expected.current_revision_sha,
            ) != self.expected:
                raise runtime_state.RuntimeStateError("execution snapshot이 변경되었습니다.")
            self.expected = current
        return current

    def check_context(self, context):
        current = self.check()
        if current is None or (
            current.plan_id != context.plan.plan_id
            or current.status != context.phase.value
            or current.active_sha != context.active_sha
            or current.worker_result != context.worker_result
            or current.worker_session_id != context.worker_session_id
            or current.worker_session_binding != context.worker_session_binding
            or current.worker_session_transition != context.worker_session_transition
            or current.validated_base_sha != context.validated_base_sha
            or current.branch != context.branch
            or current.worktree != str(context.worktree)
            or current.worker_input_token != context.worker_input_token
            or current.archive_token != context.archive_token
            or current.verify_token != context.verify_token
            or current.review_token != context.review_token
            or current.publication_token != context.publication_token
        ):
            raise runtime_state.RuntimeStateError("phase context가 최신 snapshot과 다릅니다.")

    def row(self):
        snapshot = self.check()
        if snapshot is None:
            raise runtime_state.RuntimeStateError("execution projection이 해제됐습니다.")
        project = runtime_state.project_row(self.connection, self.repo)
        # Legacy ownership helpers also consume project-owned registration fields
        # (base_branch, origin and provider). Execution fields remain authoritative
        # in the immutable plan snapshot.
        return MappingProxyType({**dict(project), **snapshot.project_values()})

    def active_execplan(self, repo):
        self.check()
        return self.repository.active_row(repo)

    def blocked(self):
        row = self.repository.get(self.repo, self.plan_id)
        return {"last_error": row.last_error}

    def run_checkpoint_publication_request(self, operation):
        """Keep attempt-local snapshots out of commit retries and cancellation."""
        from loop_manager_internal.storage.db_access import CoordinatedConnection
        if self.connection.in_transaction:
            raise runtime_state.RuntimeStateError('checkpoint publication requires transaction ownership')
        expected = self.check()

        def apply(connection):
            try:
                result = operation(connection)
                return result, self.expected
            finally:
                # COMMIT and retry waiting happen after the callable returns.
                self.expected = expected

        if isinstance(self.connection, CoordinatedConnection):
            result, updated = self.connection.run_request(apply)
        else:
            with runtime_state._transaction(self.connection):
                result, updated = apply(self.connection)
        self.expected = updated
        return result

    def recover_rebase_checkpoint(self, checkpoints, service):
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
        from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        expected = self.check()
        current = checkpoints.get(self.repo, self.plan_id)
        observed, decision = service.require_owned(checkpoint=current)
        if current.generation != 0 or decision.status is not RebaseStatus.WAITING:
            raise MutationEvidenceError('rebase recovery is no longer pending')
        if service.require_owned(checkpoint=current) != (observed, decision):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'rebase recovery observation changed')
        with runtime_state._transaction(self.connection):
            if (self.repository.projected(self.repo) != expected
                    or checkpoints.get(self.repo, self.plan_id) != current):
                raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'rebase recovery owner changed')
            checkpoints.record(
                self.repo, self.plan_id, expected_generation=0, operation='rebase',
                expected_head=service.expected.before_head,
                target_base_oid=service.expected.target_base_oid,
                observed_remote_oid=observed.remote.oid,
                snapshot={'before_head': service.expected.before_head,
                          'target_base': service.expected.target_base_oid,
                          'rebase_remote_oid': observed.remote.oid,
                          'rebase_policy': 'current_state',
                          'recovered_rebase_metadata': dict(observed.metadata)},
            )
            updated = self.repository.reserve_workspace(
                self.repo, self.plan_id, reserved_value=1,
                allowed_values=(expected.workspace_owned,))
        self.expected = updated
        return observed, decision

    def update_project(self, repo, *, preserve_transaction=False, **values):
        if repo != self.repo:
            raise runtime_state.RuntimeStateError("execution repository identity 불일치")
        # Existing callers own the commit; paired correction writes explicitly
        # retain their surrounding transaction until all evidence is stored.
        with (runtime_state._transaction(self.connection)
              if preserve_transaction else self.connection):
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
            plan_values = dict(values)
            if "phase" in plan_values:
                plan_values["status"] = runtime_state.execplan_status_for_phase(
                    plan_values.pop("phase")
                )
            self.expected = self.repository.transition_from_snapshot(
                expected,
                plan_values=plan_values, project_values=values,
            )

    def observe_worker_session(self, transition_id, session_id, binding):
        with self.connection:
            expected = self.check()
            self.expected = self.repository.observe_worker_session(
                expected, transition_id, session_id, binding,
            )

    def retain_clean_rebase_worker(self, prior, definition, checkpoints):
        """Bind proven Worker inputs to an adopted completed candidate.

        Callers must check saved correction inputs before retaining their result.
        Validation and Review remain invalidated.
        """
        if (not prior['worker_result'] or prior['worker_input_token'] != definition.worker_token
                or prior['archive_token'] != definition.archive_token):
            return
        with runtime_state._transaction(self.connection):
            self.check()
            current = checkpoints.get(self.repo, self.plan_id)
            accepted = (current.snapshot or {}).get('current_rebase_acceptance')
            from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
            if (not valid_acceptance(accepted) or current.operation is not None
                    or accepted['repo'] != self.repo or accepted['plan_id'] != self.plan_id
                    or accepted['head'] != current.expected_head
                    or accepted['target_base_oid'] != current.target_base_oid
                    or accepted['target_base_oid'] != self.expected.active_sha
                    or accepted['branch'] != self.expected.branch
                    or accepted['worktree'] != self.expected.worktree):
                return
            checkpoints.record(self.repo, self.plan_id, expected_generation=current.generation,
                operation=None, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**current.snapshot,
                          'worker_candidate_token': definition.worker_candidate_token(current.expected_head)})
            self.update_project(self.repo, preserve_transaction=True, phase='VERIFYING',
                definition_snapshot=definition.serialize(), worker_result=prior['worker_result'],
                worker_input_token=prior['worker_input_token'], archive_token=prior['archive_token'],
                revision_resume_status=None)

    def confirm_existing_pr_push(self, checkpoints, head):
        """Advance the owned PR receipt and clear its push intent atomically."""
        if checkpoints is None or checkpoints.connection is not self.connection:
            raise runtime_state.RuntimeStateError('PR publication checkpoint database differs')
        with runtime_state._transaction(self.connection):
            expected = self.check()
            current = checkpoints.get(self.repo, self.plan_id)
            if (expected is None or expected.status != 'PUBLISHING' or not expected.pr_url
                    or current.operation != 'push' or current.expected_head != head
                    or (current.snapshot or {}).get('remote_before') != expected.publication_remote_oid
                    or expected.pr_head_oid != expected.publication_remote_oid):
                raise runtime_state.RuntimeStateError('PR publication intent ownership changed')
            values = {'publication_remote_oid': head, 'pr_head_oid': head}
            updated = self.repository.transition_from_snapshot(
                expected, plan_values=values, project_values=values)
            checkpoints.record(self.repo, self.plan_id, expected_generation=current.generation,
                operation=None, expected_head=head, target_base_oid=current.target_base_oid,
                observed_remote_oid=head,
                snapshot={**(current.snapshot or {}), 'pushed_checkpoint_head': head})
        self.expected = updated

    def correct_checkpoint(self, *, phase, active_sha, validated_base_sha,
                           invalidate_validation, expected_catalog=None):
        with self.connection:
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
            self.expected = self.repository.correct_checkpoint(
                expected, phase=phase, active_sha=active_sha,
                validated_base_sha=validated_base_sha,
                invalidate_validation=invalidate_validation,
                expected_catalog=expected_catalog,
            )

    def complete_rebase(self, checkpoint_repository, *, completion=None, project_values,
                        published_proof=None, acceptance=None, reobserve=None):
        # This port publishes an in-memory snapshot and must own the commit.
        if self.connection.in_transaction:
            raise runtime_state.RuntimeStateError(
                "rebase completion requires transaction ownership")
        observed_state = None
        if acceptance is not None and callable(reobserve):
            observed_state = (self.check(), checkpoint_repository.get(self.repo, self.plan_id))
            confirmed = reobserve()
            reobserve = lambda: confirmed
        with runtime_state._transaction(self.connection):
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
            if observed_state is not None and observed_state != (
                    expected, checkpoint_repository.get(self.repo, self.plan_id)):
                raise runtime_state.RuntimeStateError("rebase 재관측 중 execution 또는 checkpoint가 변경되었습니다.")
            updated = self.repository.complete_rebase(
                expected, checkpoint_repository, completion=completion,
                project_values=project_values, published_proof=published_proof,
                acceptance=acceptance, reobserve=reobserve,
            )
            from loop_manager_internal.storage import recovery_state
            recovery_state.RecoveryRepository(
                self.connection,
            ).resolve_rebase_evidence_failure(
                self.repo, self.plan_id, base_revision=expected.active_sha,
                worker_session=expected.worker_session_id,
            )
        self.expected = updated

    def complete_rebase_publication(self, checkpoints, *, intent, acceptance, reobserve):
        """Atomically advance the lease without approving or unblocking a candidate."""
        if checkpoints.connection is not self.connection:
            raise runtime_state.RuntimeStateError('intermediate publication database differs')
        expected = self.check()
        before = checkpoints.get(self.repo, self.plan_id)
        oid, checked = reobserve()
        with runtime_state._transaction(self.connection):
            if self.check() != expected or checkpoints.get(self.repo, self.plan_id) != before:
                raise runtime_state.RuntimeStateError('intermediate publication snapshot changed')
            if (expected is None or expected.repo != intent['repo']
                    or expected.plan_id != intent['plan_id']
                    or expected.branch != intent['branch'] or expected.worktree != intent['worktree']
                    or expected.workspace_owned != 1
                    or (expected.pr_url or None) != (intent['acceptance']['remote']['pull_request'] or {}).get('identity')
                    or expected.publication_remote_oid not in (None, intent['remote_before'], intent['head'])):
                raise runtime_state.RuntimeStateError('intermediate publication owner changed')
            if oid != intent['head'] or checked != acceptance:
                raise runtime_state.RuntimeStateError('intermediate publication observation changed')
            before = checkpoints.get(self.repo, self.plan_id)
            completed = checkpoints.finish_rebase_publication(self.repo, self.plan_id,
                                                             intent=intent, acceptance=acceptance)
            if completed == before and expected.publication_remote_oid == intent['head']:
                return
            updated = self._record_rebase_publication(expected, intent['head'])
        self.expected = updated

    def _publication_values(self, expected, head):
        values = {'publication_remote_oid': head}
        if expected.pr_url:
            values.update(pr_head_oid=head, worker_input_token=None, archive_token=None,
                          verify_token=None, review_token=None, publication_token=None,
                          validated_base_sha=None, worker_result=None, verify_result=None,
                          review_result=None)
        if expected.pr_conflict_recovery:
            values['pr_conflict_head_oid'] = head
        return values

    def _record_rebase_publication(self, expected, head):
        values = self._publication_values(expected, head)
        return self.repository.transition_from_snapshot(
            expected, plan_values=values, project_values=values)

    def open_pull_request(self, repo, *, context=None, new_pr=False, **values):
        if repo != self.repo:
            raise runtime_state.RuntimeStateError("execution repository identity 불일치")
        with runtime_state._transaction(self.connection):
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
            if new_pr:
                from loop_manager_internal.notifications import notifications
                notifications.record_pr_creation(self.connection, repo, self.plan_id, values.get('pr_url'))
            result = self.repository.transition_from_snapshot(
                expected,
                plan_values={"status": "PR_OPEN", "last_error": None, **values},
                project_values=runtime_state.clear_project_values(),
            )
            from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
            manual = getattr(self, 'manual', None)
            stage_tokens = manual.publication_stage_tokens(context) if manual and context is not None else None
            ManualWorkRepository(self.connection).publish_group(
                repo, self.plan_id, values, stage_tokens=stage_tokens,
            )
            self.expected = None
            return result

    def bind(self, function, *, connected=False, mutates=False, name=None):
        """Check the selected snapshot around helpers with external effects.

        Pure parsers and log sinks need no state guard. Policy helpers which own
        persistence are checked before invocation and their resulting snapshot
        becomes the next expected observation even if a later effect fails.
        Agent callbacks use update_project
        above, so an intervening phase/projection change is rejected on return.
        """
        external = name in {
            "git", "run_agent", "verify", "command", "make_worktree",
        }

        @wraps(function)
        def invoke(*args, **kwargs):
            if mutates or external:
                self.check()
            if connected:
                args = (self.connection, *args)
            try:
                result = function(*args, **kwargs)
            finally:
                if mutates:
                    # A helper may adopt the rebase before checkpoint publication
                    # fails. Recovery must observe that durable state on retry.
                    self.expected = self.repository.projected(self.repo)
            if external and not mutates:
                self.check()
            return result

        return invoke


class DirectRebasePublicationStatePort(ExecutionStatePort):
    """Publish a selected blocked plan without claiming the execution slot."""

    def __init__(self, connection, repository, repo, plan_id):
        self.connection, self.repository, self.repo = connection, repository, repo
        self.plan_id = plan_id
        self.expected = repository.get(repo, plan_id)
        projection = repository.projected(repo)
        self.project_recovery = projection is None and self.expected.status != 'PLAN_BLOCKED'
        from loop_manager_internal.recovery.project_blocked_recovery import project_slot_available
        if (projection is not None and projection != self.expected
                or self.project_recovery and not project_slot_available(connection, repo, plan_id)):
            raise runtime_state.RuntimeStateError('direct publication execution is not selected')
        self.projected = projection is not None

    def check(self):
        from loop_manager_internal.recovery.project_blocked_recovery import project_slot_available
        if (self.repository.get(self.repo, self.plan_id) != self.expected
                or self.repository.projected(self.repo) != (self.expected if self.projected else None)
                or self.project_recovery and not project_slot_available(self.connection, self.repo, self.plan_id)):
            raise runtime_state.RuntimeStateError('direct publication execution changed')
        return self.expected

    def _record_rebase_publication(self, expected, head):
        if self.projected:
            return super()._record_rebase_publication(expected, head)
        return self.repository.update_plan(
            self.repo, self.plan_id, plan_values=self._publication_values(expected, head),
            expected_status=expected.status, enqueue=False)
