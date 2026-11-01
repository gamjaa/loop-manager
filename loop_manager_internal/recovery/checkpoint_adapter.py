"""Checkpoint safety, Git publication and durable result adapters."""
from __future__ import annotations

from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner

from collections.abc import Callable
from dataclasses import dataclass, replace
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.agent_scope_decision import _non_default_index_flags
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.agents.agent_scope_decision import _worker_path_allowed
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import blocked_checkpoint_state
from pathlib import Path
import json
import hashlib
import os
import re
import secrets
import sqlite3
import tempfile


@dataclass(frozen=True)
class PushDiagnosticCheckpointEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]


@dataclass(frozen=True)
class CheckpointSafetyEffects:
    """Read-only ownership, path scope and current evidence gates."""
    _agent_git_resources: Callable[..., object]
    _owned_checkpoint_remote_oid: Callable[..., object]
    _proven_manager_base_refs: Callable[..., object]
    _proven_merged_cleanup_paths: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    current_scope_effects: Callable[[], project_blocked_recovery.CurrentScopeEffects]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    pr_for_branch: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _frozen_push_target(url: str) -> tuple[tuple[str, str], str]:
    """Make this push command's longest URL rewrites resolve to the checked URL."""
    operand = f'x{secrets.token_hex(16)}://target'
    return ('-c', f'url.{url}.insteadOf={operand}',
            '-c', f'url.{url}.pushInsteadOf={operand}'), operand


class CheckpointSafetyAdapter:
    def diagnostic_source(self, request):
        """Prove the selected current scope even while its owned rebase waits."""
        from loop_manager_internal.recovery.diagnostic_preservation import PreservationSource, artifact_observation, redact_detail
        from loop_manager_internal.recovery.blocked_plan_reconciliation import slot_available
        plan = self._plan(request)
        project_block = scoped_blocking.project_block(self.connection, request.repo)
        if project_block is not None and not self.project_recovery_scope:
            raise RuntimeError('프로젝트 안전 차단의 연결 계획만 진단 보존할 수 있습니다.')
        block = scoped_blocking.project_block(self.connection, request.repo) if self.project_recovery_scope else scoped_blocking.plan_block(
            self.connection, request.repo, request.plan_id)
        if block is None or not slot_available(self.connection, request.repo, request.plan_id):
            raise RuntimeError('진단 보존의 원인 또는 변경 실행 자리를 확인할 수 없습니다.')
        cause = dict(block)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(request.repo, request.plan_id)
        target = checkpoint.target_base_oid or plan['active_sha'] or plan['current_revision_sha'] or plan['plan_sha']
        if not target or not request.worktree or not request.branch or plan['agent_recovery']:
            raise RuntimeError('진단 보존의 target·작업 공간 또는 Agent 범위를 확인할 수 없습니다.')
        observations = self.connection.execute(
            'SELECT branch_oid FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
            (request.repo, request.plan_id)).fetchone()
        remote = observations['branch_oid'] if observations else checkpoint.observed_remote_oid
        service = self.safety_effects.current_scope_effects()._current_rebase_service(
            self.connection, self.repo_path, self.state_dir, request.repo, request.plan_id,
            Path(request.worktree), request.branch, target=target, remote_oid=remote)
        def observe():
            try:
                return service.require_owned(checkpoint=execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(request.repo, request.plan_id))[0]
            except manager_git_journal.MutationEvidenceError:
                return artifact_observation(service)
        observed = observe()
        metadata = dict(observed.metadata or ())
        worktree = Path(request.worktree)
        snapshot = self.safety_effects.capture_git_snapshot(worktree)
        def stash_identity():
            current = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(request.repo, request.plan_id)
            stash = (current.snapshot or {}).get('stash_oid') if current.operation == 'rebase' else None
            if stash and (not isinstance(stash, str) or not re.fullmatch(r'[0-9a-fA-F]{7,64}', stash)):
                raise RuntimeError('진단 autostash identity가 안전하지 않습니다.')
            if metadata:
                for directory in ('rebase-merge', 'rebase-apply'):
                    path = self.safety_effects.git(worktree, 'rev-parse', '--git-path', directory + '/autostash')
                    location = Path(path.stdout.strip())
                    location = location if location.is_absolute() else worktree / location
                    if location.is_symlink():
                        raise RuntimeError('진단 autostash identity가 안전하지 않습니다.')
                    if location.is_file():
                        pending = location.read_text(encoding='ascii').strip()
                        if not re.fullmatch(r'[0-9a-fA-F]{7,64}', pending) or stash and stash != pending:
                            raise RuntimeError('진단 autostash identity가 일치하지 않습니다.')
                        stash = pending
            return stash
        stash = stash_identity()
        revisions = dict(base=target, head=snapshot.head,
                         start=metadata.get('orig-head', snapshot.head))
        if stash:
            revisions.update(rebase_file_evidence.stash_originals(worktree, stash, self.safety_effects.git))
            before = metadata.get('orig-head') or checkpoint.expected_head
            if not before or revisions['stash-base'] != before:
                raise RuntimeError('진단 autostash의 시작 commit이 관리 rebase와 다릅니다.')
        replay = self.safety_effects.git(worktree, 'rev-parse', '--verify', 'REBASE_HEAD^{commit}', check=False)
        if metadata and replay.returncode == 0:
            revisions['replay'] = replay.stdout.strip()
        selection_base = target
        if metadata:
            selection_base = self.safety_effects.git(worktree, 'merge-base', target, revisions['start']).stdout.strip()
        selection_revisions = [revisions['start']] if metadata else [snapshot.head]
        remaining = self.safety_effects.git(worktree, 'rev-list', '--reverse',
                                           target + '..' + revisions['start']).stdout.splitlines()
        # Include original versions of commits still to replay, without making
        # their entire trees/history reachable from the orphan diagnostic.
        for index, oid in enumerate(remaining):
            revisions['local-' + str(index)] = oid
        selection_revisions.extend(oid for oid in remaining if oid not in selection_revisions)
        stashed_untracked = set()
        if revisions.get('untracked-original'):
            stashed_untracked = set(filter(None, self.safety_effects.git(worktree, 'ls-tree', '-r',
                '--name-only', '-z', revisions['untracked-original']).stdout.split('\0')))
        def validate():
            current = self._plan(request)
            current_block = scoped_blocking.project_block(self.connection, request.repo) if self.project_recovery_scope else scoped_blocking.plan_block(
                self.connection, request.repo, request.plan_id)
            if (current_block is None or dict(current_block) != cause
                    or (current['branch'], current['worktree']) != (request.branch, request.worktree)
                    or not slot_available(self.connection, request.repo, request.plan_id)):
                raise RuntimeError('진단 보존의 원인·소유권이 변경됐습니다.')
            observe()
            if stash_identity() != stash:
                raise RuntimeError('진단 수집 중 autostash identity가 변경됐습니다.')
            result = self.safety_effects.capture_git_snapshot(worktree)
            origin_error = self._origin_safety_error(request, result)
            if origin_error:
                raise RuntimeError(origin_error.detail)
            return result
        approved = GitSnapshot.deserialize(plan['agent_result_snapshot']).dirty if plan['agent_result_snapshot'] else {}
        def allowed(name):
            state = snapshot.dirty.get(name)
            if name in stashed_untracked or state and 'untracked' in state[0]:
                if state is None or approved.get(name) != state:
                    return False
            return bool(service.adapters.path_allowed(name))
        scope = 'project' if self.project_recovery_scope else 'plan'
        return PreservationSource(
            request.repo, scope, request.plan_id,
            hashlib.sha256(json.dumps(cause, sort_keys=True).encode()).hexdigest(),
            request.branch, worktree, revisions,
            dict(reason=request.reason_code, evidence=redact_detail(str(block['evidence'])),
                 phase=plan['status'], operations=sorted(snapshot.operations),
                 rebase=metadata, remaining_commits=remaining,
                 selection_base=selection_base, selection_revisions=selection_revisions,
                 untracked_revision=revisions.get('untracked-original')), allowed, validate)

    def _plan(self, request: blocked_checkpoint.CheckpointRequest) -> sqlite3.Row:
        if self.project_recovery_scope:
            row = self.connection.execute(
                'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                (request.repo, request.plan_id)).fetchone()
            if (row is None or not project_blocked_recovery.project_slot_available(
                    self.connection, request.repo, request.plan_id)
                    or (row['branch'], row['worktree']) != (request.branch, request.worktree)):
                raise RuntimeError('project checkpoint의 선택 identity가 변경됐습니다.')
            return row
        row = self.connection.execute(
            "SELECT p.*, b.reason_code AS checkpoint_reason, "
            "b.evidence AS checkpoint_evidence "
            "FROM exec_plans AS p JOIN execplan_blocks AS b "
            "USING (repo, plan_id) WHERE p.repo = ? AND p.plan_id = ?",
            (request.repo, request.plan_id),
        ).fetchone()
        if (
            row is None
            or row["status"] != "PLAN_BLOCKED"
            or row["checkpoint_reason"] != request.reason_code
            or row["checkpoint_evidence"] != request.evidence
        ):
            raise RuntimeError("checkpoint 대상 PLAN_BLOCKED identity가 변경됐습니다.")
        return row




    def assess(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> blocked_checkpoint.SafetyVerdict:
        try:
            plan = self._plan(request)
        except RuntimeError as error:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "checkpoint_identity_changed",
                str(error),
                None,
                (),
            )
        if request.reason_code == BlockReasonCode.PULL_REQUEST_CLOSED.value:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "pull_request_closed",
                "닫힌 PR은 다시 열릴 때까지 게시하지 않고 기존 PLAN_BLOCKED를 보존합니다.",
                None,
                (),
            )
        if not request.branch or not request.worktree:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "managed_workspace_not_created",
                "관리 branch 또는 worktree 생성 전에 차단되어 local-only로 보존합니다.",
                None,
                (),
            )
        project_blocked = scoped_blocking.project_block(
            self.connection, request.repo
        ) is not None
        project_recovery_verified = bool(
            project_blocked and self.project_recovery_scope
            and project_blocked_recovery.checkpoint_scope_verified(
                self.safety_effects.current_scope_effects(), self.connection, self.repo_path, self.state_dir, request, plan))
        branch = str(plan["branch"] or "")
        worktree = Path(str(plan["worktree"] or ""))
        identity_matches = bool(
            request.branch
            and request.worktree
            and request.branch == branch
            and Path(request.worktree) == worktree
        )
        snapshot = None
        try:
            self.safety_effects.validate_execplan_workspace_identity(
                self.state_dir,
                request.repo,
                manual_workspace_owner(self.connection, request.repo, request.plan_id),
                branch,
                worktree,
            )
            self.safety_effects.validate_managed_worktree(
                self.repo_path,
                worktree,
                branch,
                allow_rebase=self.safety_effects._rebase_in_progress_for_branch(worktree, branch),
            )
            snapshot = self.safety_effects.capture_git_snapshot(worktree)
        except (OSError, RuntimeError, WorkspaceOwnershipError):
            identity_matches = False

        if (
            snapshot is not None
            and identity_matches
            and not project_blocked
            and controlling_git_operation(snapshot.operations)
        ):
            operations = tuple(sorted(snapshot.operations))
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "git_operation_in_progress",
                "미해결 Git operation: " + ", ".join(operations),
                snapshot.head,
                (),
            )

        dirty_paths: list[blocked_checkpoint.DirtyPath] = []
        shared_metadata_owned = False
        head_oid = snapshot.head if snapshot is not None else None
        actual_branch = snapshot.branch if snapshot is not None else None
        operations = tuple(sorted(snapshot.operations)) if snapshot is not None else ()
        if snapshot is not None:
            metadata_error = (self._origin_safety_error(request, snapshot)
                              if project_recovery_verified else self._shared_safety_error(request, plan, snapshot))
            if metadata_error is not None:
                return metadata_error
            active_key = str(_canonical_path(worktree))
            worktree_entry = snapshot.worktrees.get(active_key, ())
            branch_ref = f"refs/heads/{branch}"
            shared_metadata_owned = bool(
                snapshot.shared_metadata_complete
                and f"branch={branch_ref}" in worktree_entry
                and snapshot.refs.get(branch_ref, (None, None))[0] == snapshot.head
            )
            try:
                sources = tuple(str(item) for item in json.loads(str(plan["sources"])))
            except (TypeError, ValueError):
                sources = ()
            archive_sources = tuple(
                source
                for source in sources
                if source.startswith("docs/requirements/")
                and "/archive/" not in source
            )
            guidance_targets = ()
            allow_common_plans = self.safety_effects._published_plan_allows_common_plans(
                self.repo_path, plan
            )
            for path, (kinds, _) in sorted(snapshot.dirty.items()):
                target = worktree / path
                try:
                    tree_entry = self.safety_effects.git(worktree, "ls-tree", "HEAD", "--", path).stdout
                    symlink = (
                        target.is_symlink()
                        or tree_entry.startswith("120000 ")
                        or any(
                            entry.split()[1:2] == ["120000"]
                            for entry in snapshot.index.get(path, ())
                        )
                    )
                    external_worktree = bool(
                        target.exists()
                        and not target.is_file()
                        and not symlink
                    )
                except OSError:
                    symlink = True
                    external_worktree = True
                ignored_result = self.safety_effects.git(
                    worktree,
                    "check-ignore",
                    "--no-index",
                    "--quiet",
                    "--",
                    path,
                    check=False,
                )
                ignored = ignored_result.returncode == 0
                if ignored_result.returncode not in {0, 1}:
                    ignored = True
                entries = snapshot.index.get(path, ())
                dirty_paths.append(
                    blocked_checkpoint.DirtyPath(
                        path=path,
                        kinds=kinds,
                        allowed=_worker_path_allowed(
                            path,
                            str(plan["path"]),
                            archive_sources,
                            guidance_targets,
                            allow_common_plans,
                        ),
                        symlink=symlink,
                        ignored=ignored,
                        index_flags=_non_default_index_flags(entries),
                        external_worktree=external_worktree,
                    )
                )
        observation = blocked_checkpoint.SafetyObservation(
            workspace_identity_matches=identity_matches,
            workspace_owned=int(plan["workspace_owned"] or 0) == WORKSPACE_OWNED,
            head_oid=head_oid,
            actual_branch=actual_branch,
            expected_branch=branch,
            operations=operations,
            unmerged_index=bool(snapshot and any(entry.split()[-1:] != ['0']
                for entries in snapshot.index.values() for entry in entries)),
            dirty_paths=tuple(dirty_paths),
            shared_metadata_owned=shared_metadata_owned,
            project_blocked=project_blocked,
            project_recovery_verified=project_recovery_verified,
            legacy_scope_uncertain=(
                request.reason_code
                == BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value
                and not project_recovery_verified
            ),
            agent_scope_recovery_pending=not project_recovery_verified and bool(
                plan["agent_recovery"]
            ),
        )
        return blocked_checkpoint.assess_safety(observation)


    def _validated_reconciliation_scope(self, request, plan) -> bool:
        if not self.reconciliation_scope:
            return False
        project = self.connection.execute(
            'SELECT base_branch FROM projects WHERE repo = ?', (request.repo,)).fetchone()
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
            request.repo, request.plan_id)
        try:
            self.safety_effects.validate_blocked_agent_scope(
                self.connection, self.repo_path, request.repo, project['base_branch'],
                Path(request.worktree), plan['path'], saved_plan=plan,
                accepted_rebase=bool((checkpoint.snapshot or {}).get('current_rebase_acceptance')))
        except (OSError, RuntimeError, ValueError):
            return False
        return True


    def _validated_worker_checkpoint(
        self, request: blocked_checkpoint.CheckpointRequest,
        plan: sqlite3.Row, current: GitSnapshot | None,
    ) -> bool:
        if (
            plan["agent_role"] != "worker"
            or current is None
            or not plan["agent_result_snapshot"]
        ):
            return False
        worktree = Path(str(request.worktree))
        try:
            before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
            result = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
            original = before
            checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
                request.repo, request.plan_id)
            before = agent_scope_decision.manager_comparison_baseline(
                original, before, checkpoint.snapshot or {}, 'worker')
            result = agent_scope_decision.manager_comparison_baseline(
                original, result, checkpoint.snapshot or {}, 'worker')
            project = self.connection.execute(
                'SELECT base_branch FROM projects WHERE repo = ?', (request.repo,)).fetchone()
            self.safety_effects.validate_agent_scope(
                "worker", worktree, before, result,
                selected_plan_path=str(plan["path"]),
                allow_common_plans=self.safety_effects._published_plan_allows_common_plans(self.repo_path, plan),
                external_worktree=self.repo_path, base_branch=project['base_branch'],
                connection=self.connection,
            )
            if _same_local_snapshot(result, current):
                return True
            # A crash can occur after commit but before recording the Manager
            # snapshot. Accept only the durable prepared tree and identity.
            if current.dirty:
                return False
            proofs = [(request.tree_oid, request.commit_identity)]
            row = blocked_checkpoint_state.scope_request_row(self.connection, request.repo, request.plan_id)
            if row is not None and row['request_identity'] == request.request_identity:
                for entry in json.loads(row['publication_history']):
                    prior = entry.get('superseded_request', {})
                    if (prior.get('local_oid') == current.head
                            and (prior.get('branch'), prior.get('worktree'), prior.get('pr_url'))
                            == (request.branch, request.worktree, request.pr_url)):
                        proofs.append((prior.get('tree_oid'), prior.get('commit_identity')))
            return any(tree and identity
                and self.safety_effects.git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() == tree
                and f"Checkpoint-Identity: {identity}" in
                    self.safety_effects.git(worktree, "show", "-s", "--format=%B", "HEAD").stdout
                for tree, identity in proofs)
        except (RuntimeError, ValueError, TypeError, KeyError):
            return False


    def _origin_safety_error(self, request, current):
        """Registered fetch and push destinations are never a project exception."""
        project = self.connection.execute('SELECT origin FROM projects WHERE repo = ?',
                                          (request.repo,)).fetchone()
        if project is None or not project['origin']:
            return blocked_checkpoint.SafetyVerdict(
                False, blocked_checkpoint.SafetyScope.PLAN, 'registered_origin_missing',
                '등록 origin이 없어 checkpoint를 local-only로 보존합니다.', current.head, ())
        try:
            self._validated_push_url(request)
        except RuntimeError as error:
            return blocked_checkpoint.SafetyVerdict(
                False, blocked_checkpoint.SafetyScope.PROJECT, 'publication_ownership_uncertain',
                str(error), current.head, ())
        return None


    def _validated_push_url(self, request) -> str:
        """Resolve the registered destination before freezing it for push."""
        project = self.connection.execute('SELECT origin FROM projects WHERE repo = ?',
                                          (request.repo,)).fetchone()
        if project is None or not project['origin']:
            raise RuntimeError('등록 origin이 없어 checkpoint를 local-only로 보존합니다.')
        try:
            for context in (self.repo_path, Path(request.worktree)):
                for arguments in (('remote', 'get-url', '--all', 'origin'),
                                  ('remote', 'get-url', '--push', '--all', 'origin')):
                    urls = self.safety_effects.git(context, *arguments).stdout.splitlines()
                    if len(urls) != 1 or self.safety_effects.github_repository(urls[0]) != project['origin']:
                        raise RuntimeError('등록 origin과 fetch/push 대상이 다릅니다.')
                    if (context == Path(request.worktree)
                            and arguments == ('remote', 'get-url', '--push', '--all', 'origin')):
                        push_url = urls[0]
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f'checkpoint 원격 저장소 검증 실패: {error}') from error
        return push_url


    def _shared_safety_error(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        plan: sqlite3.Row,
        current: GitSnapshot,
    ) -> blocked_checkpoint.SafetyVerdict | None:
        """Check persisted ownership before any GitHub or remote operation."""
        return self._origin_safety_error(request, current)


    def existing_pull_request(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> object | None:
        if not request.branch:
            return None
        try:
            plan = self._plan(request)
            if (plan['pr_url'] or None) != request.pr_url:
                raise blocked_checkpoint.CheckpointPushError(
                    'checkpoint의 저장된 PR identity가 변경됐습니다.', retryable=False)
            return self.safety_effects.pr_for_branch(self.repo_path, request.branch, request.pr_url)
        except blocked_checkpoint.CheckpointPushError:
            raise
        except RuntimeError as error:
            raise blocked_checkpoint.CheckpointPushError(
                f"checkpoint PR 조회 실패: {error}", retryable=True
            ) from error


    @property
    def safety_effects(self) -> CheckpointSafetyEffects:
        return self._safety_effects()


@dataclass(frozen=True)
class CheckpointPublicationEffects:
    """Local preparation and exact-lease publication effects."""
    _git_with_index: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    command: Callable[..., object]
    git: Callable[..., object]


class CheckpointPublicationAdapter:
    def _record_guidance_checkpoint_scope(
        self, request: blocked_checkpoint.CheckpointRequest,
    ) -> None:
        if request.reason_code != GUIDANCE_BLOCK_REASON:
            return
        previous = self.connection.execute(
            'SELECT agent_snapshot FROM exec_plans WHERE repo = ? AND plan_id = ?',
            (request.repo, request.plan_id)).fetchone()
        before = GitSnapshot.deserialize(previous['agent_snapshot']) if previous and previous['agent_snapshot'] else None
        current = self.publication_effects.capture_git_snapshot(Path(str(request.worktree)))
        # Manager commit은 같은 호출의 기준선만 전진시킨다. 격리 증거를 새로 만들지 않는다.
        snapshot = replace(current, scope_id=before.scope_id if before else None,
                           scope_start_digest=before.scope_start_digest if before else None).serialize()
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET agent_snapshot = ?, agent_result_snapshot = ? "
                "WHERE repo = ? AND plan_id = ?",
                (snapshot, snapshot, request.repo, request.plan_id),
            )
            self.connection.execute(
                "UPDATE projects SET agent_snapshot = ?, agent_result_snapshot = ? "
                "WHERE repo = ? AND plan_path = "
                "(SELECT path FROM exec_plans WHERE repo = ? AND plan_id = ?)",
                (snapshot, snapshot, request.repo, request.repo, request.plan_id),
            )


    def _tree_for_paths(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> str:
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-checkpoint-index-"
        ) as directory:
            temporary_index = Path(directory) / "index"
            self.publication_effects._git_with_index(worktree, temporary_index, "read-tree", "HEAD")
            if paths:
                self.publication_effects._git_with_index(
                    worktree,
                    temporary_index,
                    "add",
                    "--all",
                    "--",
                    *paths,
                )
            return self.publication_effects._git_with_index(
                worktree, temporary_index, "write-tree"
            ).stdout.strip()


    def prepare(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        verdict: blocked_checkpoint.SafetyVerdict,
    ) -> blocked_checkpoint.PreparedCheckpoint:
        if not verdict.safe or verdict.head_oid is None or request.worktree is None:
            raise RuntimeError("안전하지 않은 checkpoint를 준비할 수 없습니다.")
        worktree = Path(request.worktree)
        tree_oid = self._tree_for_paths(worktree, verdict.included_paths)
        if (request.pr_url and not verdict.included_paths and request.commit_identity
                and request.tree_oid == tree_oid):
            message = self.publication_effects.git(
                worktree, 'show', '-s', '--format=%B', 'HEAD').stdout
            if (request.local_oid == verdict.head_oid
                    or f'Checkpoint-Identity: {request.commit_identity}' in message.splitlines()):
                # Keep the same durable candidate after commit/push interrupted
                # before its SQLite receipt; no new identity or empty commit.
                return blocked_checkpoint.PreparedCheckpoint(
                    verdict.head_oid, tree_oid, request.commit_identity, message, ())
        identity = blocked_checkpoint.checkpoint_identity(
            plan_id=request.plan_id,
            reason_code=request.reason_code,
            evidence=request.evidence,
            parent_oid=verdict.head_oid,
            tree_oid=tree_oid,
            included_paths=verdict.included_paths,
        )
        return blocked_checkpoint.PreparedCheckpoint(
            parent_oid=verdict.head_oid,
            tree_oid=tree_oid,
            commit_identity=identity,
            message=blocked_checkpoint.checkpoint_message(
                plan_id=request.plan_id,
                reason_code=request.reason_code,
                evidence=request.evidence,
                identity=identity,
                included_paths=verdict.included_paths,
            ),
            included_paths=verdict.included_paths,
        )


    def ensure_local_commit(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        prepared: blocked_checkpoint.PreparedCheckpoint,
    ) -> str:
        if request.worktree is None:
            raise RuntimeError("checkpoint worktree가 저장되지 않았습니다.")
        worktree = Path(request.worktree)
        current_head = self.publication_effects.git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
        if current_head != prepared.parent_oid:
            current_tree = self.publication_effects.git(
                worktree, "rev-parse", "HEAD^{tree}"
            ).stdout.strip()
            current_message = self.publication_effects.git(
                worktree, "show", "-s", "--format=%B", "HEAD"
            ).stdout
            if (
                current_tree == prepared.tree_oid
                and f"Checkpoint-Identity: {prepared.commit_identity}"
                in current_message
            ):
                self._record_guidance_checkpoint_scope(request)
                return current_head
            raise RuntimeError(
                "checkpoint 준비 뒤 관리 branch HEAD가 예상하지 못하게 변경됐습니다."
            )
        if not prepared.included_paths:
            head_tree = self.publication_effects.git(
                worktree, "rev-parse", "HEAD^{tree}"
            ).stdout.strip()
            if head_tree != prepared.tree_oid:
                raise RuntimeError("clean checkpoint tree가 현재 HEAD와 다릅니다.")
            self._record_guidance_checkpoint_scope(request)
            return current_head

        self.publication_effects.git(
            worktree,
            "add",
            "--all",
            "--",
            *prepared.included_paths,
        )
        actual_tree = self.publication_effects.git(worktree, "write-tree").stdout.strip()
        if actual_tree != prepared.tree_oid:
            raise RuntimeError(
                "checkpoint stage 결과가 사전 검증한 tree와 다릅니다."
            )
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-checkpoint-message-"
        ) as directory:
            message_path = Path(directory) / "message"
            message_path.write_text(prepared.message, encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "Loop Manager",
                    "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
                    "GIT_COMMITTER_NAME": "Loop Manager",
                    "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
                }
            )
            committed = self.publication_effects.command(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={directory}",
                    "-c",
                    "commit.gpgSign=false",
                    "-C",
                    str(worktree),
                    "commit",
                    "--file",
                    str(message_path),
                ],
                check=False,
                env=environment,
            )
        if committed.returncode:
            raise RuntimeError(
                "checkpoint commit 실패:\n" + committed.stdout[-8000:]
            )
        local_oid = self.publication_effects.git(
            worktree, "rev-parse", "HEAD^{commit}"
        ).stdout.strip()
        if self.publication_effects.git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() != prepared.tree_oid:
            raise RuntimeError("checkpoint commit tree 검증에 실패했습니다.")
        self._record_guidance_checkpoint_scope(request)
        return local_oid


    def observe_remote(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> str | None:
        if not request.branch:
            raise RuntimeError("checkpoint branch가 저장되지 않았습니다.")
        result = self.publication_effects.git(
            self.repo_path,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{request.branch}",
            check=False,
        )
        if result.returncode:
            raise blocked_checkpoint.CheckpointPushError(
                "checkpoint 원격 branch 조회 실패:\n" + result.stdout[-8000:],
                retryable=True,
            )
        oid = manager_git_journal.parse_remote_branch_oid(
            result.stdout, f'refs/heads/{request.branch}')
        history = blocked_checkpoint_state.scope_request_row(self.connection, request.repo, request.plan_id)
        if (oid is not None and oid != request.expected_remote_oid and history is not None
                and json.loads(history['publication_history'] or '[]')):
            plan = self._plan(request)
            current = self.safety_effects.capture_git_snapshot(Path(request.worktree))
            current = replace(current, refs={**current.refs,
                f'refs/remotes/origin/{request.branch}': (oid, '')})
            owned = self.safety_effects._owned_checkpoint_remote_oid(
                self.connection, self.repo_path, request, plan, current)
            if owned == oid:
                blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).accept_superseded_publication(
                    request, oid)
        if request.pr_url:
            project = self.connection.execute(
                'SELECT base_branch FROM projects WHERE repo = ?', (request.repo,)).fetchone()
            if not blocked_checkpoint.checkpoint_pr_matches(
                    self.existing_pull_request(request), url=request.pr_url,
                    branch=request.branch, head=oid, base=project['base_branch']):
                raise blocked_checkpoint.CheckpointPushError(
                    '기존 PR identity, 상태 또는 원격 head가 변경됐습니다.', retryable=False)
        return oid


    def is_fast_forward(self, remote_oid: str, local_oid: str) -> bool:
        result = self.publication_effects.git(
            self.repo_path,
            "merge-base",
            "--is-ancestor",
            remote_oid,
            local_oid,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError("checkpoint fast-forward 관계를 확인할 수 없습니다.")
        return result.returncode == 0


    def confirm(self, request, local_oid):
        self._validated_push_url(request)
        self._check_publication(request, local_oid, blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.NONE))
        return self.observe_remote(request)

    def _check_publication(self, request, local_oid, decision):
        verdict = self.assess(request)
        if not verdict.safe or verdict.head_oid != local_oid or verdict.included_paths:
            raise blocked_checkpoint.CheckpointPushError(
                verdict.detail or 'checkpoint 후보 HEAD가 변경됐습니다.', retryable=False)
        remote = self.observe_remote(request)
        project = self.connection.execute(
            'SELECT base_branch FROM projects WHERE repo = ?', (request.repo,)).fetchone()
        if not blocked_checkpoint.checkpoint_pr_matches(
                self.existing_pull_request(request), url=request.pr_url,
                branch=request.branch, head=remote, base=project['base_branch']):
            raise blocked_checkpoint.CheckpointPushError(
                '기존 PR identity, 상태 또는 head가 변경됐습니다.', retryable=False)
        if remote not in (request.expected_remote_oid, local_oid):
            raise blocked_checkpoint.CheckpointPushError(
                'checkpoint의 정확한 원격 lease가 변경됐습니다.', retryable=False)
        if decision.mode is blocked_checkpoint.PushMode.REBASE_LEASE and request.pr_url:
            from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
            current = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
                request.repo, request.plan_id)
            accepted = current.current_rebase_acceptance()
            if (not valid_acceptance(accepted) or accepted['repo'] != request.repo
                    or accepted['plan_id'] != request.plan_id
                    or accepted['branch'] != request.branch
                    or accepted['worktree'] != str(_canonical_path(Path(request.worktree)))
                    or accepted['remote']['oid'] != request.expected_remote_oid
                    or not accepted['remote']['pull_request']
                    or accepted['remote']['pull_request']['identity'] != request.pr_url
                    or not self.is_fast_forward(accepted['head'], local_oid)):
                raise blocked_checkpoint.CheckpointPushError(
                    '기존 PR checkpoint의 현재 rebase 완료 근거가 없습니다.', retryable=False)
        return remote

    def push(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        local_oid: str,
        decision: blocked_checkpoint.PushDecision,
    ) -> None:
        if not request.branch or not request.worktree:
            raise blocked_checkpoint.CheckpointPushError(
                "checkpoint branch/worktree가 저장되지 않았습니다.",
                retryable=False,
            )
        try:
            push_url = self._validated_push_url(request)
        except RuntimeError as error:
            raise blocked_checkpoint.CheckpointPushError(str(error), retryable=False) from error
        remote = (self._check_publication(request, local_oid, decision)
                  if getattr(request, 'pr_url', None) else None)
        rewrite_options, operand = _frozen_push_target(push_url)
        arguments = [
            "push",
            operand,
            f"{local_oid}:refs/heads/{request.branch}",
        ]
        if decision.mode in {blocked_checkpoint.PushMode.CREATE, blocked_checkpoint.PushMode.FAST_FORWARD}:
            arguments.insert(1, manager_git_journal.exact_branch_lease(
                request.branch, request.expected_remote_oid))
        if decision.mode is blocked_checkpoint.PushMode.REBASE_LEASE:
            if not request.expected_remote_oid:
                raise blocked_checkpoint.CheckpointPushError(
                    "rebase checkpoint lease OID가 없습니다.", retryable=False
                )
            arguments.insert(
                1,
                manager_git_journal.exact_branch_lease(request.branch, request.expected_remote_oid),
            )
        tracking_ref = f"refs/remotes/origin/{request.branch}"
        tracked = self.publication_effects.git(
            Path(request.worktree), "rev-parse", "--verify", tracking_ref, check=False,
        )
        tracked_oid = tracked.stdout.strip() if tracked.returncode == 0 else "0" * len(local_oid)
        scoped_options = (("-c", f"core.hooksPath={os.devnull}")
                          if self.project_recovery_scope else ())
        if remote != local_oid:
            pushed = self.publication_effects.git(
                Path(request.worktree), *scoped_options, *rewrite_options, *arguments, check=False)
            if pushed.returncode:
                raise blocked_checkpoint.CheckpointPushError(
                    "checkpoint push 실패:\n" + pushed.stdout[-8000:],
                    retryable=blocked_checkpoint.push_failure_retryable(pushed.stdout),
                )
        # A URL push does not refresh origin's tracking ref as a named-remote
        # push does. Preserve that local result without overwriting a concurrent
        # ref change; the durable checkpoint can recover after a failed CAS.
        updated = self.publication_effects.git(
            Path(request.worktree), "update-ref", tracking_ref, local_oid, tracked_oid,
            check=False,
        )
        if updated.returncode:
            raise RuntimeError(
                "checkpoint push 뒤 origin 추적 ref 갱신 실패:\n" + updated.stdout[-8000:]
            )


    @property
    def publication_effects(self) -> CheckpointPublicationEffects:
        return self._publication_effects()


class BlockedCheckpointAdapter(CheckpointSafetyAdapter, CheckpointPublicationAdapter):
    """Bind one identity to independent safety and publication ports."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repo_path: Path,
        state_dir: Path,
        repo: str,
        *, reconciliation_scope: bool = False,
        project_recovery_scope: bool = False,
        safety_effects: Callable[[], CheckpointSafetyEffects],
        publication_effects: Callable[[], CheckpointPublicationEffects],
    ):
        self._safety_effects = safety_effects
        self._publication_effects = publication_effects
        self.connection = connection
        self.repo_path = repo_path
        self.state_dir = state_dir
        self.repo = repo
        self.reconciliation_scope = reconciliation_scope
        self.project_recovery_scope = project_recovery_scope


def delete_diagnostic_checkpoint(git, adapter, request, expected_oid, *, target_digest, revalidate):
    """Delete only a pinned diagnostic ref at the freshly validated destination.

    The caller owns the ref lock and durable cleanup intent. This effect does
    not turn a successful push into a confirmed absence or a journal transition.
    """
    if (not request.branch or not request.branch.startswith('loop-manager/diagnostics/')
            or not isinstance(expected_oid, str)
            or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', expected_oid) is None):
        raise RuntimeError('진단 삭제 ref 또는 저장된 게시 OID가 유효하지 않습니다.')
    origin = adapter._validated_push_url(request)
    if hashlib.sha256(origin.encode()).hexdigest() != target_digest:
        raise RuntimeError('진단 삭제의 관측 후 게시 대상이 변경됐습니다.')
    revalidate()
    options, operand = _frozen_push_target(origin)
    pushed = git(Path(request.worktree), '-c', f'core.hooksPath={os.devnull}',
        *options, 'push', manager_git_journal.exact_branch_lease(request.branch, expected_oid),
        operand, f':refs/heads/{request.branch}', check=False)
    if pushed.returncode:
        detail = pushed.stdout[-8000:]
        raise blocked_checkpoint.CheckpointPushError('진단 ref 삭제 실패: ' + detail,
            retryable=blocked_checkpoint.push_failure_retryable(detail)
                and not any(marker in detail.casefold() for marker in (
                    'stale info', 'fetch first', 'hook declined', 'remote rejected')))


def push_diagnostic_checkpoint(effects: PushDiagnosticCheckpointEffects, adapter, request, head):
    """Create an exact absent ref; never update original or tracking refs."""
    if not request.branch or not request.branch.startswith('loop-manager/diagnostics/'):
        raise RuntimeError('진단 ref 이름이 유효하지 않습니다.')
    origin = adapter._validated_push_url(request)
    rewrite_options, operand = _frozen_push_target(origin)
    # A frozen URL operand avoids Git's automatic remote-tracking update.
    pushed = effects.git(Path(request.worktree), '-c', f'core.hooksPath={os.devnull}',
                 *rewrite_options, 'push',
                 manager_git_journal.exact_branch_lease(request.branch, None), operand,
                 f'{head}:refs/heads/{request.branch}', check=False)
    if pushed.returncode:
        raise blocked_checkpoint.CheckpointPushError(
            '진단 ref 게시 실패: ' + pushed.stdout[-8000:],
            retryable=blocked_checkpoint.push_failure_retryable(pushed.stdout))


class _BlockedCheckpointResults:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def promote_project_block(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        verdict: blocked_checkpoint.SafetyVerdict,
    ) -> None:
        if scoped_blocking.project_block(self.connection, request.repo) is not None:
            # A checkpoint refusal cannot replace an independent safety cause.
            return
        reason = {
            "shared_ref_mutation": BlockReasonCode.SHARED_REF_MUTATION,
            "shared_git_config_mutation": BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
            "outside_managed_worktree_mutation": BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
            "workspace_ownership_uncertain": (
                BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN
            ),
            "publication_ownership_uncertain": BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
        }.get(
            str(verdict.reason_code),
            BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                request.repo,
                reason,
                verdict.detail or "checkpoint project safety violation",
                request.request_identity,
                plan_id=request.plan_id,
                previous_phase=None,
            )

    def finalized(self, request: blocked_checkpoint.CheckpointRequest) -> None:
        # The repository commits the result and outbox together. There must be
        # no separate, crash-sensitive notification transaction here.
        pass
