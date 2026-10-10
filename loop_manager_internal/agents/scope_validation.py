"""Role result validation and invocation-bound Git resource proof."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import re

from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentScopeError, AgentChange
from loop_manager_internal.agents.agent_scope_decision import _worker_path_allowed
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage import execution_checkpoint_state, agent_invocation_state
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, BlockScope
from loop_manager_internal.git import managed_rebase_validation
from loop_manager_internal.git.base_fetch_service import reconcile_external_base
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService, selected_baseline


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
    """Narrow scope only for a complete ledger bound to this saved execution."""
    if not before.scope_id or not before.scope_start_digest or not before.shared_metadata_complete:
        return None
    repo = str(repo_path)
    try:
        project = effects.repository(connection).scope_owner(repo)
        if project is None:
            return None
        owner = project
        saved = GitSnapshot.deserialize(str(owner['agent_snapshot'])) if owner['agent_snapshot'] else None
        plan_id = effects._plan_id_from_path(selected_plan_path or str(project['plan_path'] or ''))
        if saved is None or saved.scope_id != before.scope_id:
            owner = (effects.repository(connection).scope_owner(repo, plan_id)
                     if plan_id is not None else None)
            saved = (GitSnapshot.deserialize(str(owner['agent_snapshot']))
                     if owner is not None and owner['agent_snapshot'] else None)
        if (saved is None or saved.scope_id != before.scope_id
                or saved.scope_start_digest != before.scope_start_digest
                or owner['agent_role'] != role or owner['worktree'] != str(worktree)):
            return None
        if role in {'planner', 'planner_check', 'maintenance_planner'}:
            plan_id = None
        session_key = 'planner_session_id' if role in {'planner', 'maintenance_planner'} else f'{role}_session_id'
        session_id = (agent_invocation_state.InvocationRepository(connection).latest_scope_session(before.scope_id)
                      if role == 'planner_check' else owner[session_key])
        invocation_role = role
        if role == 'maintenance_planner':
            # Maintenance units own their sessions in their own state tables;
            # the provider executes the same Planner policy in this unique scope.
            invocation_role = 'planner'
            session_id = agent_invocation_state.InvocationRepository(connection).latest_scope_session(before.scope_id)
        common = effects._git_common_directory(worktree)
        if common != effects._git_common_directory(repo_path):
            return None
        origin = effects.git(worktree, 'remote', 'get-url', 'origin').stdout.strip()
        repository_identity = hashlib.sha256((str(common) + '\0' + origin).encode()).hexdigest()
        if not agent_invocation_state.InvocationRepository(connection).verified_scope(
            before.scope_id, repo=repo, plan_id=plan_id, role=invocation_role,
            provider=str(project['provider']), session_id=session_id,
            worktree=str(_canonical_path(worktree)), repository_identity=repository_identity,
            start_digest=before.scope_start_digest,
        ):
            return None
        phase = str(owner['phase'] if 'phase' in owner.keys() else owner['status'])
        if phase == 'PROJECT_BLOCKED':
            safety = scoped_blocking.project_block(connection, repo)
            phase = str(safety['previous_phase'] or '') if safety is not None else ''
        owned_branch = owner['branch'] if role in {'worker', 'reviewer'} else saved.branch
        if role == 'reviewer' and phase in {'WORKING', 'IMPLEMENTING'} and plan_id:
            # A base refresh invalidates the candidate stages before the
            # completed read-only Reviewer scope has been retired. Keep its
            # invocation-bound monitoring only with a live, owned rebase.
            state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
            if state_dir is not None:
                checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
                _, verdict = effects._current_rebase_service(
                    connection, repo_path, state_dir, repo, plan_id, worktree,
                    owned_branch).require_owned(checkpoint=checkpoint)
                if verdict.status is managed_rebase_validation.RebaseStatus.COMPLETE:
                    phase = 'REVIEWING'
        resources = agent_scope_decision.used_git_resources(
            role=role, phase=phase, base_branch=str(project['base_branch']),
            branch=owned_branch, worktree_key=str(_canonical_path(worktree)),
        )
    except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError, RuntimeError, OSError):
        return None
    if resources is not None:
        effects.validate_managed_worktree(repo_path, worktree, owned_branch,
                                  allow_rebase=effects._rebase_in_progress_for_branch(worktree, owned_branch))
    return resources


def _completed_worker_rebase_baseline(
    role: str, worktree: Path, before: GitSnapshot, after: GitSnapshot,
    selected_plan_path: str | None, repo_path: Path | None,
    connection: sqlite3.Connection | None, effects: ScopeValidationEffects,
) -> GitSnapshot:
    """Accept only a live, owned completion of a scope begun during rebase.

    The current-result service checks files, index, inputs, reservation and
    remote/PR leases. Other refs, worktrees and config retain their old baseline.
    A saved result from a different Git state cannot borrow this acceptance.
    """
    if (role != 'worker' or connection is None or repo_path is None
            or not selected_plan_path or before.branch is not None
            or not {'rebase-merge', 'rebase-apply'}.intersection(before.operations)
            or after.branch is None):
        return before
    plan_id = effects._plan_id_from_path(selected_plan_path)
    if not plan_id:
        return before
    repo = str(repo_path)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
    if (checkpoint.operation != 'rebase' and not managed_rebase_validation.valid_acceptance(
            (checkpoint.snapshot or {}).get('current_rebase_acceptance'))):
        return before
    plan = effects.repository(connection).scope_owner(repo, plan_id)
    state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
    if plan is None or state_dir is None:
        return before
    try:
        live = effects.capture_git_snapshot(worktree)
        if not agent_scope_decision._same_local_snapshot(live, after):
            return before
        observed, verdict = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree,
            plan['branch']).require_owned(checkpoint=checkpoint)
        if verdict.status is not managed_rebase_validation.RebaseStatus.COMPLETE:
            return before
        if effects.capture_git_snapshot(worktree) != live:
            return before
        key, ref = str(_canonical_path(worktree)), f"refs/heads/{plan['branch']}"
        if (observed.head != after.head
                or not agent_scope_decision._same_local_snapshot(live, after)
                or live.refs.get(ref) != after.refs.get(ref)
                or live.worktrees.get(key) != after.worktrees.get(key)
                or observed.repository_state_fingerprint != managed_rebase_validation.fingerprint({
                    'refs': live.refs, 'worktrees': live.worktrees, 'config': live.config})):
            return before
        return selected_baseline(before, after, plan['branch'], key)
    except (RuntimeError, OSError, ValueError):
        return before


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
    """Validate the net Git mutation made by one Agent role."""

    after = effects._scope_snapshot_with_start_ignore_rules(worktree, before, after)
    if resources is None and connection is not None and external_worktree is not None:
        resources = effects._agent_git_resources(
            connection, external_worktree, worktree, before, role_name,
            selected_plan_path=selected_plan_path,
        )
    if external_worktree is not None and base_branch is not None:
        registered_origin = None
        if connection is not None:
            project = effects.repository(connection).scope_owner(
                str(_canonical_path(Path(external_worktree)))
            )
            if project is not None:
                registered_origin = str(project['origin'] or '')
        before = reconcile_external_base(
            effects.git, external_worktree, base_branch, before, after, resources,
            registered_origin=registered_origin,
        )
    original = before
    before = _completed_worker_rebase_baseline(
        role_name, worktree, before, after, selected_plan_path,
        external_worktree, connection, effects)
    changes = effects.agent_snapshot_changes(worktree, before, after)
    if before is not original:
        # Completion changes stage/OID normally, but never excuses security
        # flag changes, including flags removed before the final observation.
        changes.extend(AgentChange(path, ('index flags',))
                       for path in original.index.keys() | after.index.keys()
                       if agent_scope_decision.index_security_flags(original.index.get(path, ()))
                       != agent_scope_decision.index_security_flags(after.index.get(path, ())))
        owned_rebase = True
    violations: list[str] = []
    violation_reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
    external_key = (
        str(_canonical_path(external_worktree))
        if external_worktree is not None
        else None
    )
    def add_violation(message: str, reason: BlockReasonCode) -> None:
        nonlocal violation_reason
        violations.append(message)
        if scoped_blocking.reason_scope(reason) is BlockScope.PROJECT:
            violation_reason = reason

    if before.head != after.head:
        add_violation(
            f"HEAD 변경 ({before.head} -> {after.head})",
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )
    if before.branch != after.branch:
        add_violation(
            f"branch 전환 ({before.branch or 'detached'} -> "
            f"{after.branch or 'detached'})",
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )

    conflict_paths = agent_scope_decision.changed_conflict_paths(before, after)
    if conflict_paths and not owned_rebase:
        if connection is not None and external_worktree is not None and selected_plan_path:
            plan_id = effects._plan_id_from_path(selected_plan_path)
            state_dir = effects._owned_rebase_state_dir(str(external_worktree), plan_id, worktree) if plan_id else None
            if state_dir is not None:
                saved = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                    str(external_worktree), plan_id)
                plan_row = effects.repository(connection).scope_owner(str(external_worktree), plan_id)
                if plan_row is not None:
                    try:
                        _, verdict = effects._current_rebase_service(
                            connection, external_worktree, state_dir, str(external_worktree),
                            plan_id, worktree, plan_row['branch']).require_owned(checkpoint=saved)
                        owned_rebase = verdict.status is managed_rebase_validation.RebaseStatus.WAITING
                    except (RuntimeError, OSError, ValueError):
                        pass
        if not owned_rebase:
            add_violation('소유 rebase를 확인할 수 없는 충돌 index 변경 ('
                          + ', '.join(sorted(conflict_paths)[:8]) + ')',
                          BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION)

    if not before.shared_metadata_complete or not after.shared_metadata_complete:
        add_violation(
            "공유 Git metadata 기준 snapshot 누락",
            BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )
    else:
        scope_before, scope_after = agent_scope_decision.scoped_metadata(
            before, after, resources,
            default_worktree=external_key,
            base_branch=base_branch,
        )
        if resources is not None:
            excluded_refs = sorted(key for key in set(before.refs) | set(after.refs)
                if key not in resources.refs and before.refs.get(key) != after.refs.get(key))
            excluded_worktrees = sorted(key for key in set(before.worktrees) | set(after.worktrees)
                if key not in resources.worktrees and before.worktrees.get(key) != after.worktrees.get(key))
            excluded_config = sorted(key for key in set(before.config) | set(after.config)
                if key not in scope_before.config and key not in scope_after.config
                and before.config.get(key) != after.config.get(key))
            if excluded_refs or excluded_worktrees or excluded_config:
                effects.operation_log(f"Agent 범위 밖 독립 Git 변화: refs={excluded_refs[:8]} "
                              f"worktrees={excluded_worktrees[:8]} config={excluded_config[:8]}")
        # Codex owns this ephemeral namespace for turn capture/checkpoint
        # bookkeeping. It never identifies a published branch or a managed
        # workspace, so normalize both sides for current and persisted scope
        # snapshots while keeping every other repository ref protected.
        before_scope_refs = {
            key: value
            for key, value in scope_before.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        after_scope_refs = {
            key: value
            for key, value in scope_after.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        metadata_states = (
            (
                "공유 Git refs",
                before_scope_refs,
                after_scope_refs,
                allowed_ref_changes,
                BlockReasonCode.SHARED_REF_MUTATION,
            ),
            (
                "Git worktree 등록·HEAD·index·파일 상태",
                scope_before.worktrees,
                scope_after.worktrees,
                allowed_worktree_changes,
                BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
            ),
            (
                "Git config",
                scope_before.config,
                scope_after.config,
                frozenset(),
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
            ),
            (
                "Git operation 상태",
                before.operations,
                after.operations,
                frozenset(),
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
            ),
        )
        for label, before_state, after_state, allowed_changes, reason in metadata_states:
            changed_keys = sorted(
                key
                for key in set(before_state) | set(after_state)
                if before_state.get(key) != after_state.get(key)
                and key not in allowed_changes
            )
            if changed_keys:
                if label == "공유 Git refs":
                    owned_branch_ref = (
                        f"refs/heads/{before.branch}" if before.branch else None
                    )
                    unowned_changes = [
                        key for key in changed_keys if key != owned_branch_ref
                    ]
                    if unowned_changes:
                        reason = BlockReasonCode.SHARED_REF_MUTATION
                    else:
                        reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
                elif label == "Git worktree 등록·HEAD·index·파일 상태":
                    managed_worktree = str(_canonical_path(worktree))
                    unowned_changes = [
                        key for key in changed_keys if key != managed_worktree
                    ]
                    if external_key is not None and external_key in unowned_changes:
                        reason = BlockReasonCode.DEFAULT_WORKTREE_MUTATION
                    elif unowned_changes:
                        reason = BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION
                    else:
                        reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
                displayed = ", ".join(changed_keys[:8])
                remainder = len(changed_keys) - 8
                if remainder > 0:
                    displayed += f", 외 {remainder}개"
                add_violation(f"{label} 변경 ({displayed})", reason)

    for change in changes:
        if "index flags" in change.kinds:
            add_violation(
                f"{change.path} ({', '.join(change.kinds)})",
                BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            )
            continue
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
                    if known or re.search(r"^draft:", current_text, re.MULTILINE) or "## User Decision" in current_text:
                        previous_text = revision_document_or_none(effects.git, worktree, before.head, change.path)
                        plan = execplan_catalog.parse_execplan_text(change.path, current_text, validate_sources=False)
                        execplan_drafts.validate_agent_change(plan, previous_text)
                        if plan.get("draft") and (not previous_text or execplan_catalog.parse_frontmatter(previous_text).get("draft") != "true"):
                            raise RuntimeError("Worker는 실행한 계획을 draft로 전환할 수 없습니다.")
                except (RuntimeError, OSError, UnicodeError) as error:
                    add_violation(f"{change.path} ({error})", BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION)
        elif role_name in {"reviewer", "planner_check"}:
            allowed = False
        else:
            raise ValueError(f"알 수 없는 Agent 역할입니다: {role_name}")
        if not allowed:
            add_violation(
                f"{change.path} ({', '.join(change.kinds) or 'Git 상태'})",
                BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            )

    if violations:
        raise AgentScopeError(
            f"{role_name} Git 범위 위반: " + "; ".join(violations),
            result_snapshot=after,
            reason_code=violation_reason,
        )
