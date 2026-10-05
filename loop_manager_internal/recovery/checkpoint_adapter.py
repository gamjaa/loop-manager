"""Checkpoint safety, Git publication and durable result adapters."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.agent_scope_decision import _non_default_index_flags
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.agents.agent_scope_decision import _worker_path_allowed
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.documents.guidance_approval import content_hash as guidance_content_hash
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.storage import execution_checkpoint_state
from pathlib import Path
import json
import os
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
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_rows: Callable[..., object]
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


    def _approved_guidance_targets(self, request: blocked_checkpoint.CheckpointRequest) -> tuple[str, ...]:
        targets = []
        for row in self.connection.execute(
            "SELECT target_path, expected_content_hash FROM guidance_approvals "
            "WHERE repo = ? AND plan_id = ? AND applied = 1 "
            "AND expected_content_hash IS NOT NULL ORDER BY target_path",
            (request.repo, request.plan_id),
        ):
            target = str(row["target_path"])
            path = Path(str(request.worktree or "")) / target
            try:
                matches = (
                    path.is_file()
                    and not path.is_symlink()
                    and guidance_content_hash(path.read_bytes())
                    == str(row["expected_content_hash"])
                )
            except OSError:
                matches = False
            if matches:
                targets.append(target)
        return tuple(targets)


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
        if plan["pr_url"]:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "existing_pull_request",
                "기존 PR에 연결된 계획은 checkpoint 대상이 아닙니다.",
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
                request.plan_id,
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
            and snapshot.operations
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
            guidance_targets = self._approved_guidance_targets(request)
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
                or (
                    plan["agent_snapshot"]
                    and not self._validated_guidance_checkpoint(request, plan, snapshot)
                    and not self._validated_reconciliation_scope(request, plan)
                )
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


    def _validated_guidance_checkpoint(
        self, request: blocked_checkpoint.CheckpointRequest,
        plan: sqlite3.Row, current: GitSnapshot | None,
    ) -> bool:
        if (
            request.reason_code != GUIDANCE_BLOCK_REASON
            or plan["agent_role"] != "worker"
            or current is None
            or not plan["agent_result_snapshot"]
        ):
            return False
        rows = self.safety_effects._guidance_rows(self.connection, request.repo, request.plan_id)
        if not rows or any(row["invalidated_decision_hash"] for row in rows):
            return False
        worktree = Path(str(request.worktree))
        try:
            document = self.safety_effects._guidance_document(worktree, str(plan["path"]))
            if document is None or document.structure_hash != plan["guidance_plan_hash"]:
                return False
            self.safety_effects._guidance_document_matches_rows(document, rows, require_decisions=False)
            before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
            result = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
            project = self.connection.execute(
                'SELECT base_branch FROM projects WHERE repo = ?', (request.repo,)).fetchone()
            self.safety_effects.validate_agent_scope(
                "worker", worktree, before, result,
                selected_plan_path=str(plan["path"]),
                allowed_guidance_targets=self._approved_guidance_targets(request),
                allow_common_plans=self.safety_effects._published_plan_allows_common_plans(self.repo_path, plan),
                external_worktree=self.repo_path, base_branch=project['base_branch'],
                connection=self.connection,
            )
            if _same_local_snapshot(result, current):
                return True
            # A crash can occur after commit but before recording the Manager
            # snapshot. Accept only the durable prepared tree and identity.
            return bool(
                request.commit_identity and request.tree_oid
                and not current.dirty
                and self.safety_effects.git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() == request.tree_oid
                and f"Checkpoint-Identity: {request.commit_identity}" in
                self.safety_effects.git(worktree, "show", "-s", "--format=%B", "HEAD").stdout
            )
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
                False, blocked_checkpoint.SafetyScope.PROJECT, 'shared_git_config_mutation',
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
        def reject(reason, detail):
            return blocked_checkpoint.SafetyVerdict(
                False, blocked_checkpoint.SafetyScope.PROJECT,
                reason, detail, current.head, (),
            )

        origin_error = self._origin_safety_error(request, current)
        if origin_error is not None:
            return origin_error

        serialized = plan["agent_snapshot"] or plan["agent_result_snapshot"]
        try:
            if not serialized:
                raise ValueError("persisted Git snapshot이 없습니다.")
            before = GitSnapshot.deserialize(str(serialized))
            if not before.shared_metadata_complete:
                raise ValueError("persisted shared metadata가 없습니다.")
            resources = self.safety_effects._agent_git_resources(
                self.connection, self.repo_path, Path(request.worktree), before,
                str(plan['agent_role'] or ''), selected_plan_path=str(plan['path']))
            before, current = agent_scope_decision.scoped_metadata(before, current, resources)
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            return reject("workspace_ownership_uncertain", str(error))
        if before.config != current.config:
            return reject("shared_git_config_mutation", "checkpoint Git config가 저장된 snapshot과 다릅니다.")
        own_ref = f"refs/heads/{request.branch}"
        remote_ref = f"refs/remotes/origin/{request.branch}"
        owned_remote_oid = (
            self.safety_effects._owned_checkpoint_remote_oid(
                self.connection, self.repo_path, request, plan, current,
            )
            if before.refs.get(remote_ref) != current.refs.get(remote_ref)
            else None
        )
        fetched_refs = self.safety_effects._proven_manager_base_refs(
            self.connection, self.repo_path, request.repo, request.plan_id,
            before, current,
        )
        for name in before.refs.keys() | current.refs.keys():
            if name == own_ref or name in fetched_refs or name.startswith("refs/codex/turn-diffs/"):
                continue
            if before.refs.get(name) == current.refs.get(name):
                continue
            if name == remote_ref and current.refs.get(name) == (owned_remote_oid, ""):
                continue
            return reject("shared_ref_mutation", f"checkpoint 공유 ref 변경: {name}")
        own_path = str(_canonical_path(Path(request.worktree)))
        cleaned = self.safety_effects._proven_merged_cleanup_paths(
            self.connection, request.repo, before, current,
        )
        for path in before.worktrees.keys() | current.worktrees.keys():
            if (path != own_path and path not in cleaned
                    and before.worktrees.get(path) != current.worktrees.get(path)):
                return reject("outside_managed_worktree_mutation", f"checkpoint 외부 worktree 변경: {path}")
        return None


    def existing_pull_request(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> object | None:
        if not request.branch:
            return None
        try:
            return self.safety_effects.pr_for_branch(self.repo_path, request.branch)
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
        return manager_git_journal.parse_remote_branch_oid(
            result.stdout, f'refs/heads/{request.branch}')


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
