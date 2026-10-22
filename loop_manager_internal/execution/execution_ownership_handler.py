"""Ownership execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
import sqlite3

from loop_manager_internal.execution.execution_session import ExecutionIdentity
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

@dataclass(frozen=True)
class OwnershipHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    checkpoint: ExecutionCheckpointRepository | None
    state: ExecutionStatePort
    current_rebase_service: Callable[..., Any] | None
    git: Callable[..., Any]
    remote_branch_oid: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _completed_autostash_correction: Callable[..., Any]
    _current_rebase_service: Callable[..., Any]
    _rebase_journal_state: Callable[..., Any]
    _row: Callable[[], Mapping[str, object]]


class OwnershipHandler:
    """Ownership policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: OwnershipHandlerPorts):
        self.ports = ports

    def _owned_checkpoint_remote(
        self, context, row, remote_oid, current, pull_request=None,
        *, rebase_observation=None,
    ):
        """검증한 현재 작업 입력과 과거 게시 영수증을 분리한다."""
        pending_push = current is not None and current.operation == 'push' and current.expected_head == remote_oid
        # A confirmed intermediate push can precede the SQLite lease update;
        # its exact intent retains ownership across older BLOCKED receipts.
        publication = current.rebase_publication_intent() if current and current.operation == 'rebase_publish' else None
        pending_checkpoint = bool(publication and remote_oid == publication['head']
            and publication['remote_before'] == row['publication_remote_oid']
            and publication['remote_before'] == (current.snapshot or {}).get('accepted_work_branch')
            and publication['repo'] == self.ports.identity.repo
            and publication['plan_id'] == context.plan.plan_id
            and publication['branch'] == context.branch and publication['worktree'] == str(context.worktree))
        if current and remote_oid and (pending_push or pending_checkpoint or remote_oid in (
                (current.snapshot or {}).get('accepted_work_branch'),
                (current.snapshot or {}).get('pending_work_branch_input'))):
            self.ports.validate_execplan_workspace_identity(
                self.ports.identity.state_dir, self.ports.identity.repo, context.plan.plan_id,
                context.branch, context.worktree)
            self.ports.validate_managed_worktree(
                self.ports.identity.repo_path, context.worktree, context.branch,
                allow_rebase=current.operation == 'rebase')
            if (pending_push or pending_checkpoint) and self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip() != remote_oid:
                return False
            if pull_request and ((row['pr_url'] and pull_request.get('url') != row['pr_url'])
                    or pull_request.get('headRefName') != context.branch
                    or pull_request.get('headRefOid') != remote_oid):
                return False
            try:
                live = manager_git_journal.live_remote_branch_oid(
                    self.ports.git, self.ports.identity.repo_path, context.branch)
            except (RuntimeError, OSError) as error:
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY, '현재 원격 작업 branch 관측을 재시도합니다.') from error
            if live != remote_oid:
                raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '현재 원격 입력이 검증 중 변경됐습니다.')
            return True
        try:
            checkpoint = self.ports.state.connection.execute(
                'SELECT * '
                'FROM blocked_checkpoints '
                'WHERE repo = ? AND plan_id = ?',
                (self.ports.identity.repo, context.plan.plan_id),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint database observation failed') from error
        if checkpoint is None:
            return True
        if checkpoint['phase'] != 'published':
            # 게시 checkpoint 이력이 없는 기존 PR은 별도 PR 게시 gate가 소유권 원본이다.
            if checkpoint['publication_history'] == '[]' and (
                    not row['publication_remote_oid']
                    or (row['pr_url'] and not checkpoint['published_oid'])
                    or (not checkpoint['published_oid'] and not checkpoint['accepted_remote_oid']
                        and checkpoint['expected_remote_oid'] is None and not checkpoint['remote_observed'])):
                return True
            saved = (current.snapshot or {}) if current else {}
            resume = saved.get('published_review_resume')
            owner = resume.get('owner_oid') if isinstance(resume, dict) else None
            checkpoint = checkpoint_ownership.publication_receipt(
                checkpoint, owner or row['publication_remote_oid'])
            if checkpoint is None:
                return False
        if current is not None and current.operation == 'rebase':
            return self._owned_current_checkpoint_rebase(
                context, row, remote_oid, current, checkpoint, pull_request,
                rebase_observation=rebase_observation)
        if current is not None and ((current.snapshot or {}).get('current_rebase_acceptance')
                or (current.snapshot or {}).get('completed_rebase')
                or (current.snapshot or {}).get('rebase_completion')
                or self._legacy_current_rebase(context, current)):
            return self._owned_current_checkpoint_rebase(
                context, row, remote_oid, current, checkpoint, pull_request,
                rebase_observation=rebase_observation,
            )
        review_resume = (current.snapshot or {}).get('published_review_resume') if current else None
        if review_resume is not None and current.operation != 'rebase':
            return self._owned_published_review_resume(
                context, row, remote_oid, current, checkpoint,
                review_resume, pull_request,
            )
        if current and (current.snapshot or {}).get('remote_adoption_scope'):
            return self._owned_adopted_checkpoint(
                context, row, remote_oid, current, checkpoint, pull_request)
        marker = (current.snapshot or {}).get('blocked_resume') if current else None
        if marker:
            return self._owned_current_checkpoint_rebase(
                context, row, remote_oid, current, checkpoint, pull_request)
        if not marker and row['publication_remote_oid']:
            if current is None or current.operation not in {None, 'commit'}:
                return False
            # OID 일치로 승인하지 않고 기존 게시·현재 공간·실제 원격 검증을 거친다.
            return self._owned_published_review_resume(
                context, row, remote_oid, current, checkpoint,
                {'request_identity': checkpoint['request_identity'],
                 'owner_oid': checkpoint['accepted_remote_oid'],
                 'head': checkpoint['accepted_remote_oid']}, pull_request)
        if not marker and not row['publication_remote_oid']:
            # A reviewed correction is Worker input only when its remote head
            # is still the checkpoint's last accepted, owned ref.
            if (row['pr_url'] or pull_request is not None
                    or remote_oid != checkpoint['accepted_remote_oid']):
                return False
            try:
                self.ports.validate_execplan_workspace_identity(
                    self.ports.identity.state_dir, self.ports.identity.repo, context.plan.plan_id,
                    context.branch, context.worktree,
                )
                self.ports.validate_managed_worktree(
                    self.ports.identity.repo_path, context.worktree, context.branch,
                )
                local_oid = self.ports.git(
                    context.worktree, 'rev-parse', 'HEAD',
                ).stdout.strip()
            except WorkspaceOwnershipError:
                return False
            except (RuntimeError, OSError) as error:
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'checkpoint worktree observation failed') from error
            if local_oid != remote_oid:
                return False
            return checkpoint_ownership.owned_remote_oid(
                self.ports.state.connection, self.ports.identity.repo_path, self.ports.identity.repo,
                context.plan.plan_id, context.branch, context.worktree,
                remote_oid, self.ports.git,
                before_head=local_oid, recorded_oid=remote_oid,
                strict_observation=True,
            ) == remote_oid
        return False

    def _owned_adopted_checkpoint(self, context, row, remote_oid, current,
                                  checkpoint, pull_request):
        """Keep a checked fast-forward adoption owned through Worker and push."""
        snapshot = current.snapshot or {}
        scope = snapshot.get('remote_adoption_scope')
        accepted = checkpoint['accepted_remote_oid']
        recorded = row['publication_remote_oid']
        if (not isinstance(scope, dict) or not accepted
                or scope.get('head') != accepted or scope.get('remote') != accepted
                or snapshot.get('last_adopted_remote_oid') != accepted
                or scope.get('branch') != context.branch
                or scope.get('worktree') != str(context.worktree)
                or checkpoint['branch'] != context.branch
                or checkpoint['worktree'] != str(context.worktree)
                or snapshot.get('remote_adoption_intent')):
            return False
        try:
            self.ports.validate_execplan_workspace_identity(
                self.ports.identity.state_dir, self.ports.identity.repo, context.plan.plan_id,
                context.branch, context.worktree)
            self.ports.validate_managed_worktree(
                self.ports.identity.repo_path, context.worktree, context.branch)
            local = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
            if self.ports.git(self.ports.identity.repo_path, 'rev-parse',
                              f'refs/heads/{context.branch}').stdout.strip() != local:
                return False
            if (current.operation == 'commit' and local != current.expected_head
                    and not checkpoint_ownership.committed_tree_matches_intent(
                        self.ports.identity.repo_path, current.expected_head, local,
                        snapshot, self.ports.git, strict_observation=True)):
                return False
            if checkpoint_ownership.owned_remote_oid(
                    self.ports.state.connection, self.ports.identity.repo_path,
                    self.ports.identity.repo, context.plan.plan_id, context.branch,
                    context.worktree, accepted, self.ports.git, before_head=local,
                    require_live=False, strict_observation=True, allow_history=True) != accepted:
                return False
            before = snapshot.get('remote_before')
            pending_push = (current.operation == 'push' and bool(before)
                and before in (accepted, snapshot.get('pushed_checkpoint_head'))
                and current.expected_head == local == remote_oid
                and recorded in (before, remote_oid))
            confirmed_push = (snapshot.get('pushed_checkpoint_head') == remote_oid == recorded)
            if not ((remote_oid == recorded == accepted) or pending_push or confirmed_push):
                return False
            ancestor = before if pending_push else remote_oid
            if ancestor != accepted and self.ports.git(
                    context.worktree, 'merge-base', '--is-ancestor', ancestor, local,
                    check=False).returncode != 0:
                return False
            if row['pr_url'] and (pull_request is None
                    or pull_request.get('url') != row['pr_url']
                    or pull_request.get('headRefName') != context.branch
                    or pull_request.get('headRefOid') != remote_oid
                    or (pending_push and row['pr_head_oid'] not in (before, remote_oid))
                    or (not pending_push and row['pr_head_oid'] != remote_oid)):
                return False
            return manager_git_journal.live_remote_branch_oid(
                self.ports.git, self.ports.identity.repo_path, context.branch) == remote_oid
        except RecoveryFailure:
            raise
        except WorkspaceOwnershipError:
            return False
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'adopted checkpoint ownership observation failed') from error

    def _owned_published_review_resume(
        self, context, row, remote_oid, current, checkpoint, marker, pull_request,
    ):
        """Recheck the original published owner without inventing rebase proof."""
        original = str(checkpoint['accepted_remote_oid'] or '')
        recorded = str(row['publication_remote_oid'] or '')
        if (
            not isinstance(marker, dict) or not original
            or marker.get('request_identity') != checkpoint['request_identity']
            or marker.get('owner_oid') != original
            or marker.get('head') != original
            or checkpoint['branch'] != context.branch
            or checkpoint['worktree'] != str(context.worktree)
            or (current.snapshot or {}).get('blocked_resume')
            or row['pr_url']
            or (pull_request is not None and current.operation != 'pr_create')
            or (pull_request is not None
                and pull_request.get('headRefOid') != remote_oid)
        ):
            return False
        try:
            self.ports.validate_execplan_workspace_identity(
                self.ports.identity.state_dir, self.ports.identity.repo, context.plan.plan_id,
                context.branch, context.worktree,
            )
            self.ports.validate_managed_worktree(
                self.ports.identity.repo_path, context.worktree, context.branch,
            )
            local = self.ports.git(
                context.worktree, 'rev-parse', 'HEAD',
            ).stdout.strip()
            branch_ref = self.ports.git(
                self.ports.identity.repo_path, 'rev-parse', f'refs/heads/{context.branch}',
            ).stdout.strip()
            if local != branch_ref:
                return False
            if (not (current.snapshot or {}).get('published_review_resume')
                    and current.operation is None and local != original):
                return False
            if (current.operation == 'commit'
                    and local != current.expected_head
                    and not checkpoint_ownership.committed_tree_matches_intent(
                        self.ports.identity.repo_path, current.expected_head, local,
                        current.snapshot or {}, self.ports.git,
                        strict_observation=True,
                    )):
                return False
            if checkpoint_ownership.owned_remote_oid(
                self.ports.state.connection, self.ports.identity.repo_path, self.ports.identity.repo,
                context.plan.plan_id, context.branch, context.worktree,
                original, self.ports.git,
                before_head=local, require_live=False,
                strict_observation=True, allow_history=True,
            ) != original:
                return False
            pending_push = (
                current.operation == 'push'
                and (current.snapshot or {}).get('remote_before') == original
                and current.expected_head == local == remote_oid
                and remote_oid != original
                and recorded in (original, remote_oid)
            )
            if remote_oid == original and recorded == original:
                expected_live = original
            elif pending_push or (
                remote_oid == recorded == local
                and (current.snapshot or {}).get('pushed_checkpoint_head') == local
            ):
                expected_live = remote_oid
            else:
                return False
            observed = self.ports.git(
                self.ports.identity.repo_path, 'ls-remote', '--heads', 'origin',
                f'refs/heads/{context.branch}', check=False,
            )
        except RecoveryFailure:
            raise
        except WorkspaceOwnershipError:
            return False
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint review ownership observation failed') from error
        if observed.returncode:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint review remote observation failed')
        ref = f'refs/heads/{context.branch}'
        rows = [line.split() for line in observed.stdout.splitlines() if line.strip()]
        if rows and (len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref):
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint review remote response malformed')
        live_oid = rows[0][0] if rows else None
        return live_oid == expected_live

    def _current_rebase_service(self, context, **options):
        factory = self.ports.current_rebase_service
        if factory is None:
            raise manager_git_journal.MutationEvidenceError('current rebase adapter unavailable')
        return factory(context, **options)

    def _checkpoint_remote_oid(self, repo_path, branch):
        checkpoints = self.ports.checkpoint
        current = (checkpoints.get(self.ports.identity.repo, self.ports.state.plan_id)
                   if checkpoints is not None else None)
        return managed_rebase_adoption.checkpoint_remote_oid(
            current, repo_path, branch, git=self.ports.git,
            read_fetched=self.ports.remote_branch_oid)

    def _owned_current_checkpoint_rebase(self, context, row, remote_oid, current,
                                         checkpoint, pull_request, *, rebase_observation=None):
        original = str(checkpoint['accepted_remote_oid'] or '')
        recorded = str(row['publication_remote_oid'] or original)
        if not original:
            return False
        intermediate = (current.snapshot or {}).get('rebase_publication_intent') or {}
        pending_intermediate = (current.operation == 'rebase_publish'
                                and intermediate.get('remote_before') == recorded
                                and remote_oid in (recorded, intermediate.get('head')))
        before = (current.snapshot or {}).get('remote_before')
        adopted = (current.snapshot or {}).get('last_adopted_remote_oid') or original
        # Final push recovery can run after the runtime row adopted its head,
        # but before the checkpoint intent was cleared. Keep the saved lease
        # bound to the prior owned OID in either ordering.
        pending_push = (current.operation == 'push'
                        and before in (recorded, adopted)
                        and recorded in (before, current.expected_head)
                        and remote_oid == current.expected_head)
        pushed = ((current.snapshot or {}).get('pushed_checkpoint_head') == remote_oid
                  and recorded == remote_oid)
        if not ((remote_oid == original and recorded == original) or pending_push or pushed
                or pending_intermediate
                or (remote_oid == recorded and (current.snapshot or {}).get('last_adopted_remote_oid') == recorded)):
            return False
        if pull_request is not None and (
                pull_request.get('headRefOid') != remote_oid
                or (row['pr_url'] and pull_request.get('url') != row['pr_url'])
                or (not row['pr_url'] and current.operation != 'pr_create')):
            return False
        try:
            if checkpoint_ownership.owned_remote_oid(
                self.ports.state.connection, self.ports.identity.repo_path, self.ports.identity.repo,
                context.plan.plan_id, context.branch, context.worktree,
                original, self.ports.git, recorded_oid=original,
                require_live=remote_oid == original,
                strict_observation=True, allow_history=True,
            ) != original:
                return False
            service = self.ports._current_rebase_service(context, remote_oid=remote_oid)
            if pull_request is not None:
                # The pending PR creation is checked separately by publication
                # recovery; bind its exact head here without granting a push.
                from loop_manager_internal.git.managed_rebase_validation import PullRequest
                service = replace(service, expected=replace(
                    service.expected, remote=replace(service.expected.remote,
                        pull_request=PullRequest(str(pull_request['url']), self.ports.identity.repo,
                            context.branch, remote_oid, 'OPEN'))))
            # 이 호출 구간에서 전달된 증거만 사용한다. 새 service의 전체 입력과
            # generation이 다르면 다시 관측하며 실행 단계 사이에 저장하지 않는다.
            if (rebase_observation is not None
                    and rebase_observation[0] == service.expected
                    and service.expected.generation == current.generation):
                _, observed, decision = rebase_observation
            else:
                observed, decision = service.observe(checkpoint=current)
            if decision.status is RebaseStatus.UNAVAILABLE:
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'checkpoint rebase observation unavailable')
            # START only means the saved target could be applied; it does not
            # seal ownership of a published rebase whose base is not an ancestor.
            if decision.status not in {RebaseStatus.WAITING, RebaseStatus.COMPLETE}:
                return False
            if current.operation == 'commit' and observed.head != current.expected_head:
                return checkpoint_ownership.committed_tree_matches_intent(
                    self.ports.identity.repo_path, current.expected_head, observed.head,
                    current.snapshot or {}, self.ports.git,
                    strict_observation=True)
            return True
        except RecoveryFailure:
            raise
        except manager_git_journal.MutationEvidenceError:
            return False
        except (WorkspaceOwnershipError, KeyError, TypeError):
            return False
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint rebase ownership observation failed') from error

    def _owned_detached_rebase(self, context, row, remote_oid, current, checkpoint, pull_request):
        return self._owned_current_checkpoint_rebase(
            context, row, remote_oid, current, checkpoint, pull_request)

    @staticmethod
    def _rebase_completion_session(context, current):
        snapshot = getattr(current, 'snapshot', None) or {}
        session = getattr(context, 'worker_session_id', None)
        if snapshot.get('stash_oid') and snapshot.get('pending') == 'continue':
            return snapshot.get('rebase_start_session_id', session)
        return session

    def _matching_rebase_journal_result(self, context, current, remote_oid,
                                        observed, *, allow_later_file_changes=False):
        checkpoint_snapshot = getattr(current, 'snapshot', None) or {}

        def matches(item):
            saved = item['result']['observed']
            if not allow_later_file_changes:
                return saved == observed or (
                    checkpoint_snapshot.get('stash_oid')
                    and checkpoint_snapshot.get('pending') == 'continue'
                    and self.ports._completed_autostash_correction(
                        context, item['intent'], saved, observed,
                    )
                )
            # The sealed rebase result owns Git's progress, not subsequent
            # Worker edits to conflict files and their index entries.
            return all(saved.get(field) == observed.get(field) for field in (
                'head', 'branch', 'ref', 'registration', 'operations', 'metadata',
            ))

        return any(
            item['intent']['kind'] == 'managed_rebase'
            and item['intent']['target'] == str(context.worktree.resolve())
            and (checkpoint_snapshot.get('rebase_evidence_version') != 2
                 or (item['intent']['expected'].get('branch') == context.branch
                     and item['intent']['expected'].get('run_id')
                         == checkpoint_snapshot.get('rebase_run_id')
                     and item['intent']['expected'].get('checkpoint_generation', -1)
                         <= getattr(current, 'generation', -1)))
            and item['intent']['expected']['before_head'] == current.expected_head
            and item['intent']['expected']['target_base_oid'] == current.target_base_oid
            and item['intent']['expected']['remote_oid'] == remote_oid
            and matches(item)
            for item in manager_git_journal.verified_managed_transitions(
                self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()),
                f'plan:{context.plan.plan_id}',
                agent_session_id=self._rebase_completion_session(context, current),
                git=self.ports.git,
            )
        )

    def _owned_persisted_rebase(self, context, row, remote_oid, current, checkpoint, pull_request):
        return self._owned_current_checkpoint_rebase(
            context, row, remote_oid, current, checkpoint, pull_request)

    def _completed_rebase_proof(self, context, current, remote_oid, observed):
        """Bind the SQLite completion generation to one verified journal result."""
        for item in manager_git_journal.verified_managed_transitions(
            self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()),
            f'plan:{context.plan.plan_id}',
            agent_session_id=self._rebase_completion_session(context, current),
            git=self.ports.git,
        ):
            expected = item['intent']['expected']
            if (item['intent']['kind'] == 'managed_rebase'
                    and item['intent']['target'] == str(context.worktree.resolve())
                    and expected.get('branch') == context.branch
                    and expected.get('run_id') == (current.snapshot or {}).get('rebase_run_id')
                    and expected.get('checkpoint_generation', -1) <= current.generation
                    and expected.get('before_head') == current.expected_head
                    and expected.get('target_base_oid') == current.target_base_oid
                    and expected.get('remote_oid') == remote_oid
                    and item['result']['observed'] == observed):
                proof = {
                    'journal_id': item['intent']['id'],
                    'generation': current.generation,
                    'run_id': expected.get('run_id'),
                    'before_head': current.expected_head,
                    'after_head': observed['head'],
                    'target_base_oid': current.target_base_oid,
                    'remote_oid': remote_oid,
                    'branch': context.branch,
                    'worktree': str(context.worktree.resolve()),
                    'session': self._rebase_completion_session(context, current),
                }
                snapshot = current.snapshot or {}
                if snapshot.get('stash_oid') and snapshot.get('pending') == 'continue':
                    live = self.ports._rebase_journal_state(context)
                    if not self.ports._completed_autostash_correction(
                            context, item['intent'], observed, live):
                        return None
                    proof['autostash_correction'] = {
                        'sealed': observed, 'approved': live,
                        'stash_oid': snapshot['stash_oid'],
                        'stage_evidence': snapshot.get('stage_evidence'),
                        'session': getattr(context, 'worker_session_id', None),
                    }
                return proof
        return None

    def _owned_completed_rebase(self, context, row, remote_oid, current, checkpoint,
                                pull_request, local_head):
        return self._owned_current_checkpoint_rebase(
            context, row, remote_oid, current, checkpoint, pull_request)

    def _legacy_current_rebase(self, context, current):
        # A persisted target beyond the adopted remote base identifies legacy
        # pending rebase work without inventing execution receipts. This only
        # selects the common G-109 ownership/scope check; it grants no push.
        saved = current.snapshot or {}
        target = getattr(current, 'target_base_oid', None)
        head = getattr(current, 'expected_head', None)
        remote = getattr(current, 'observed_remote_oid', None)
        if (current.operation is not None or not target or not head or not remote
                or head == remote or saved.get('current_rebase_acceptance')
                or saved.get('published_review_resume')):
            return False
        row = self.ports._row()
        if row['pr_url'] or row['pr_conflict_recovery']:
            return False
        ancestry = self.ports.git(self.ports.identity.repo_path, 'merge-base', '--is-ancestor',
            target, remote, check=False)
        if ancestry.returncode not in (0, 1):
            raise manager_git_journal.MutationEvidenceError('legacy rebase target ancestry unavailable')
        return ancestry.returncode == 1
