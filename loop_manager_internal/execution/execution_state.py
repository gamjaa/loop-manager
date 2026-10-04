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
        project = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
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

    def update_project(self, repo, **values):
        if repo != self.repo:
            raise runtime_state.RuntimeStateError("execution repository identity 불일치")
        with self.connection:
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
        with runtime_state._transaction(self.connection):
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
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
        with runtime_state._transaction(self.connection):
            expected = self.check()
            if (expected is None or expected.repo != intent['repo']
                    or expected.plan_id != intent['plan_id']
                    or expected.branch != intent['branch'] or expected.worktree != intent['worktree']
                    or expected.workspace_owned != 1 or expected.pr_url
                    or expected.publication_remote_oid not in (None, intent['remote_before'], intent['head'])):
                raise runtime_state.RuntimeStateError('intermediate publication owner changed')
            oid, checked = reobserve()
            if oid != intent['head'] or checked != acceptance:
                raise runtime_state.RuntimeStateError('intermediate publication observation changed')
            before = checkpoints.get(self.repo, self.plan_id)
            completed = checkpoints.finish_rebase_publication(self.repo, self.plan_id,
                                                             intent=intent, acceptance=acceptance)
            if completed == before and expected.publication_remote_oid == intent['head']:
                return
            updated = self._record_rebase_publication(expected, intent['head'])
        self.expected = updated

    def _record_rebase_publication(self, expected, head):
        return self.repository.transition_from_snapshot(
            expected, plan_values={'publication_remote_oid': head},
            project_values={'publication_remote_oid': head})

    def open_pull_request(self, repo, **values):
        if repo != self.repo:
            raise runtime_state.RuntimeStateError("execution repository identity 불일치")
        with self.connection:
            expected = self.check()
            if expected is None:
                raise runtime_state.RuntimeStateError("execution projection이 없습니다.")
            result = self.repository.transition_from_snapshot(
                expected,
                plan_values={"status": "PR_OPEN", "last_error": None, **values},
                project_values=runtime_state.clear_project_values(),
            )
            self.expected = None
            return result

    def bind(self, function, *, connected=False, mutates=False, name=None):
        """Check the selected snapshot around helpers with external effects.

        Pure parsers and log sinks need no state guard. Policy helpers which own
        persistence are checked before invocation and their resulting snapshot
        becomes the next expected observation. Agent callbacks use update_project
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
            result = function(*args, **kwargs)
            if mutates:
                self.expected = self.repository.projected(self.repo)
            elif external:
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
        if (projection is not None and projection != self.expected
                or projection is None and self.expected.status != 'PLAN_BLOCKED'):
            raise runtime_state.RuntimeStateError('direct publication execution is not selected')
        self.projected = projection is not None

    def check(self):
        if (self.repository.get(self.repo, self.plan_id) != self.expected
                or self.repository.projected(self.repo) != (self.expected if self.projected else None)):
            raise runtime_state.RuntimeStateError('direct publication execution changed')
        return self.expected

    def _record_rebase_publication(self, expected, head):
        if self.projected:
            return super()._record_rebase_publication(expected, head)
        return self.repository.update_plan(
            self.repo, self.plan_id, plan_values={'publication_remote_oid': head},
            expected_status=expected.status, enqueue=False)
