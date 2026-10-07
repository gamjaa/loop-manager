"""Context execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any
from pathlib import Path
import json
from loop_manager_internal.notifications import notifications
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionPhase, ExecutionPlanMetadata

from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class ContextHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    update_snapshot: Callable[..., None]
    state: ExecutionStatePort
    operation_log: Callable[..., Any]
    _resolving_rebase: Callable[..., bool]


class ContextHandler:
    """Context policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: ContextHandlerPorts):
        self.ports = ports

    def _row(self):
        return self.ports.state.row()

    def _context(self):
        row = self._row()
        catalog_plan = self.ports.state.active_execplan(self.ports.identity.repo)
        if catalog_plan is None or self.ports.snapshot().plan is None:
            raise RuntimeError('활성 ExecPlan 실행 context를 복구할 수 없습니다.')
        self.ports.update_snapshot(catalog_plan=catalog_plan)
        phase = ExecutionPhase(str(catalog_plan['status']))
        return ExecutionContext(
            repo=self.ports.identity.repo,
            repo_path=self.ports.identity.repo_path,
            state_dir=self.ports.identity.state_dir,
            base_branch=self.ports.identity.base,
            base_sha=self.ports.identity.base_sha,
            provider=self.ports.identity.provider,
            config_path=self.ports.identity.config_path,
            plan=ExecutionPlanMetadata(
                plan_id=str(self.ports.snapshot().plan['id']),
                title=str(self.ports.snapshot().plan['title']),
                path=str(row['plan_path']),
                plan_sha=str(row['plan_sha']) if row['plan_sha'] is not None else None,
                sources=tuple((str(item) for item in self.ports.snapshot().plan.get('sources', ()))),
                definition_revision_sha=(
                    str(self.ports.snapshot().catalog_plan['current_revision_sha'])
                    if self.ports.snapshot().catalog_plan['current_revision_sha'] is not None
                    else None
                ),
                content_hash=str(self.ports.snapshot().catalog_plan['content_hash']),
                depends_on=tuple(
                    str(item) for item in self.ports.snapshot().plan.get('depends_on', ())
                ),
                complexity=str(self.ports.snapshot().plan.get('complexity')),
                risk=str(self.ports.snapshot().plan.get('risk')),
                priority=str(self.ports.snapshot().plan.get('priority')),
            ),
            phase=phase,
            branch=str(row['branch'] or ''),
            worktree=Path(str(row['worktree'] or '')),
            active_sha=str(row['active_sha'] or ''),
            validated_base_sha=str(row['validated_base_sha']) if row['validated_base_sha'] is not None else None,
            worker_session_id=str(row['worker_session_id']) if row['worker_session_id'] is not None else None,
            worker_session_binding=row['worker_session_binding'],
            worker_session_transition=row['worker_session_transition'],
            reviewer_session_id=str(row['reviewer_session_id']) if row['reviewer_session_id'] is not None else None,
            worker_result=str(row['worker_result']) if row['worker_result'] is not None else None,
            verify_result=str(row['verify_result']) if row['verify_result'] is not None else None,
            review_result=str(row['review_result']) if row['review_result'] is not None else None,
            pr_url=str(row['pr_url']) if row['pr_url'] is not None else None,
            pr_conflict_recovery=bool(row['pr_conflict_recovery']),
            pr_conflict_base_oid=str(row['pr_conflict_base_oid']) if row['pr_conflict_base_oid'] is not None else None,
            resolving_rebase=self.ports._resolving_rebase(row),
            archive_sources=self.ports.snapshot().archive_sources,
            archive_snapshot_token=(
                self.ports.snapshot()._archive_snapshot.token
                if self.ports.snapshot()._archive_snapshot is not None else None
            ),
            archive_candidate_scope=self.ports.snapshot().archive_candidates,
            current_definition_token=(
                self.ports.snapshot()._definition_snapshot.definition_token
                if self.ports.snapshot()._definition_snapshot is not None
                else None
            ),
            current_base_token=str(row['active_sha'] or ''),
            worker_input_token=(
                str(row['worker_input_token'])
                if row['worker_input_token'] is not None else None
            ),
            archive_token=(
                str(row['archive_token'])
                if row['archive_token'] is not None else None
            ),
            verify_token=(
                str(row['verify_token'])
                if row['verify_token'] is not None else None
            ),
            review_token=(
                str(row['review_token'])
                if row['review_token'] is not None else None
            ),
            publication_token=(
                str(row['publication_token'])
                if row['publication_token'] is not None else None
            ),
        )

    @staticmethod
    def _cached_plan(catalog_plan):
        return {
            'id': str(catalog_plan['plan_id']),
            'title': str(catalog_plan['title']),
            'sources': list(json.loads(str(catalog_plan['sources']))),
            'depends_on': list(json.loads(str(catalog_plan['depends_on']))),
            'complexity': str(catalog_plan['current_complexity'] or 'low'),
            'risk': str(catalog_plan['current_risk'] or 'low'),
            'priority': str(catalog_plan['priority']),
        }

    def _advanced(self):
        return ExecutionOutcome.advanced(self._context())

    def work_started(self, context):
        notifications.flush_project_events(self.ports.state.connection, self.ports.identity.repo)
