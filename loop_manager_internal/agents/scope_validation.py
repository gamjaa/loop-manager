"""Role result validation and current Manager Git resource selection."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import subprocess
import re

from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentScopeError, AgentChange
from loop_manager_internal.agents.agent_scope_decision import _worker_path_allowed
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService


@dataclass(frozen=True)
class ScopeValidationEffects:
    """Explicit effects supplied by the composition boundary."""

    _agent_git_resources: Callable[..., agent_scope_decision.GitResources | None]
    repository: Callable[[sqlite3.Connection], RuntimeStateRepository]

    _current_rebase_service: Callable[..., ManagedRebaseService]
    _git_common_directory: Callable[..., Path]
    _owned_rebase_state_dir: Callable[..., Path | None]
    _plan_id_from_path: Callable[..., str | None]
    _rebase_in_progress_for_branch: Callable[..., bool]
    _scope_snapshot_with_start_ignore_rules: Callable[..., GitSnapshot]
    agent_snapshot_changes: Callable[..., list[AgentChange]]
    capture_git_snapshot: Callable[..., GitSnapshot]
    git: Callable[..., subprocess.CompletedProcess[str]]
    operation_log: Callable[..., None]
    validate_managed_worktree: Callable[..., None]


def _agent_git_resources(
    connection: sqlite3.Connection,
    repo_path: Path,
    worktree: Path,
    before: GitSnapshot,
    role: str,
    *,
    selected_plan_path: str | None = None,
    effects: ScopeValidationEffects,
) -> agent_scope_decision.GitResources | None:
    """Select current Manager resources independently of Agent attempt history."""
    project = effects.repository(connection).scope_owner(str(repo_path))
    if project is None:
        return None
    plan_id = effects._plan_id_from_path(selected_plan_path or str(project['plan_path'] or ''))
    owner = project
    if (not project['worktree'] or _canonical_path(Path(project['worktree'])) != _canonical_path(worktree)):
        owner = (effects.repository(connection).scope_owner(str(repo_path), plan_id)
                 if plan_id else None)
    if owner is None or not owner['worktree']:
        return None
    if _canonical_path(Path(owner['worktree'])) != _canonical_path(worktree):
        return None
    return agent_scope_decision.used_git_resources(
        role=role, phase=agent_scope_decision.invocation_scope_phase(role),
        base_branch=str(project['base_branch']),
        branch=owner['branch'] if role in {'worker', 'reviewer'} else None,
        worktree_key=str(_canonical_path(worktree)),
    )


def validate_agent_scope(
    role_name: str,
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    selected_plan_path: str | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    allowed_ref_changes: frozenset[str] = frozenset(),
    allowed_worktree_changes: frozenset[str] = frozenset(),
    external_worktree: Path | None = None,
    base_branch: str | None = None,
    connection: sqlite3.Connection | None = None,
    resources: agent_scope_decision.GitResources | None = None,
    owned_rebase: bool = False,
    effects: ScopeValidationEffects,
) -> None:
    """Validate publishable role output; access permissions belong to sandbox."""
    if role_name in {'reviewer', 'planner_check'}:
        return
    if role_name not in {'planner', 'maintenance_planner', 'worker'}:
        raise ValueError(f"알 수 없는 Agent 역할입니다: {role_name}")
    after = effects._scope_snapshot_with_start_ignore_rules(worktree, before, after)
    # Index flags describe Git operation safety, not published file content.
    content_index = lambda snapshot: {
        path: tuple('H ' + entry.split(' ', 1)[-1] for entry in entries)
        for path, entries in snapshot.index.items()
    }
    changes = effects.agent_snapshot_changes(
        worktree, replace(before, index=content_index(before)),
        replace(after, index=content_index(after)))
    violations: list[str] = []
    violation_reason = (BlockReasonCode.WORKER_OUTPUT_INVALID if role_name == 'worker'
                        else BlockReasonCode.PLAN_INPUT_INVALID)

    def add_violation(message: str) -> None:
        violations.append(message)

    for change in changes:
        if (change.path not in before.index and change.path not in after.index
                and change.path not in after.dirty and 'HEAD' not in change.kinds
                and 'untracked' in before.dirty.get(change.path, ((), ''))[0]):
            continue  # Removing an unpublished artifact does not publish it.
        allowed = False
        if role_name == "planner":
            allowed = bool(
                change.path == "docs/FUTURE_IDEAS.md"
                or re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
            )
        elif role_name == "maintenance_planner":
            allowed = bool(
                change.path == "docs/FUTURE_IDEAS.md"
                or re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
            )
        elif role_name == "worker":
            if selected_plan_path is None:
                raise ValueError("Worker scope 검증에 선택된 ExecPlan 경로가 없습니다.")
            allowed = _worker_path_allowed(
                change.path,
                selected_plan_path,
                allowed_requirement_archives,
                allowed_guidance_targets,
                allow_common_plans,
            )
            if allowed and re.fullmatch(r"docs/plans/\[G-\d{3,}\] [^/]+\.md", change.path):
                from loop_manager_internal.catalog import execplan_catalog, execplan_drafts
                from loop_manager_internal.git.command_adapter import revision_document_or_none
                try:
                    path = worktree / change.path
                    current_text = path.read_text(encoding="utf-8") if path.is_file() else ""
                    from loop_manager_internal.storage.execplan_draft_state import evidence
                    plan_id = effects._plan_id_from_path(change.path)
                    known = connection is not None and evidence(connection, str(external_worktree or worktree), plan_id) is not None
                    if (known or path.is_file() or change.path in before.index
                            or 'HEAD' in change.kinds
                            or after.dirty.get(change.path, ((), ''))[1] == 'missing'):
                        previous_text = revision_document_or_none(effects.git, worktree, before.head, change.path)
                        if previous_text and not path.is_file():
                            raise RuntimeError("Worker는 기존 ExecPlan의 결정·실행 기록을 파일 삭제로 제거할 수 없습니다.")
                        plan = execplan_catalog.parse_execplan_text(change.path, current_text, validate_sources=False)
                        execplan_drafts.validate_agent_change(plan, previous_text)
                        if plan.get("draft") and (not previous_text or execplan_catalog.parse_frontmatter(previous_text).get("draft") != "true"):
                            raise RuntimeError("Worker는 실행한 계획을 draft로 전환할 수 없습니다.")
                except (RuntimeError, OSError, UnicodeError) as error:
                    add_violation(f"{change.path} ({error})")
        elif role_name in {"reviewer", "planner_check"}:
            allowed = False
        else:
            raise ValueError(f"알 수 없는 Agent 역할입니다: {role_name}")
        if not allowed:
            add_violation(
                f"{change.path} ({', '.join(change.kinds) or 'Git 상태'})",
            )

    if violations:
        raise AgentScopeError(
            f"{role_name} 결과 변경 범위 오류: " + "; ".join(violations),
            result_snapshot=after,
            reason_code=violation_reason,
        )
