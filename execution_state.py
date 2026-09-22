"""Snapshot-bound adapter for execution persistence and legacy policy helpers."""

from __future__ import annotations

from functools import wraps
from types import MappingProxyType

import runtime_state


class ExecutionStatePort:
    def __init__(self, connection, repository, repo):
        self.connection = connection
        self.repository = repository
        self.repo = repo
        self.expected = repository.projected(repo)
        if self.expected is None:
            raise runtime_state.RuntimeStateError("활성 execution snapshot이 없습니다.")

    def check(self):
        current = self.repository.projected(self.repo)
        if current != self.expected:
            raise runtime_state.RuntimeStateError("execution snapshot이 변경되었습니다.")
        return current

    def check_context(self, context):
        current = self.check()
        if current is None or (
            current.plan_id != context.plan.plan_id
            or current.status != context.phase.value
            or current.active_sha != context.active_sha
            or current.worker_result != context.worker_result
            or current.validated_base_sha != context.validated_base_sha
            or current.branch != context.branch
            or current.worktree != str(context.worktree)
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
        row = self.repository.get(self.repo, self.expected.plan_id)
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
