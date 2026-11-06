"""Retire exact document-stage copies through the existing worktree cleanup port."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import re

from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
from loop_manager_internal.storage.task_calls import CallDeferred


def document_resources(snapshot, state_dir, repo, plan_id):
    """Read paths from native document-stage records, never from arbitrary strings."""
    managed = Path(state_dir).resolve() / 'worktrees' / hashlib.sha256(repo.encode()).hexdigest()[:16]
    roots = tuple(managed / folder / plan_id for folder in ('document-revisions', 'document-assessments'))
    resources = {}

    def add(path, expected, head):
        if (not isinstance(path, str) or Path(path) != expected
                or not isinstance(head, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', head)
                or path in resources and resources[path] != head):
            raise ValueError('document copy identity differs')
        resources[path] = head

    def read(item, *, historical=False):
        if item is None:
            return
        if (not isinstance(item, dict) or not historical and item.get('version') != 1
                or not isinstance(item.get('base'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', item['base'])):
            raise ValueError('unknown document-stage resource record')
        if item.get('workspace') is not None:
            add(item['workspace'], roots[0] / item['base'], item.get('commit') or item['base'])
        if item.get('planner_workspace') is not None:
            scope = item['planner_scope']
            try:
                before = GitSnapshot.deserialize(scope['before'])
            except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as error:
                raise ValueError('unknown document assessment snapshot') from error
            path = Path(item['planner_workspace'])
            if (scope['role'] != 'planner' or scope['worktree'] != str(path)
                    or path.parent != roots[1] or not re.fullmatch(r'[0-9a-f]{64}', path.name)
                    or before.branch is not None or before.dirty or before.operations):
                raise ValueError('unknown document assessment owner')
            if 'planner_generation' in item:
                expected = hashlib.sha256(f"{item['base']}:{item['candidate']}:{item['planner_generation']}".encode()).hexdigest()
                if path.name != expected:
                    raise ValueError('document assessment generation differs')
            add(str(path), roots[1] / path.name, before.head)
        history = item.get('superseded', [])
        if not isinstance(history, list):
            raise ValueError('unknown document history')
        for prior in history:
            read(prior, historical=True)

    def read_snapshot(item):
        if not isinstance(item, dict):
            raise ValueError('unknown checkpoint snapshot')
        for field in ('document_revision', 'retired_document_assessment'):
            read(item.get(field))
        history = item.get('rebase_operation_history', [])
        if not isinstance(history, list):
            raise ValueError('unknown operation history')
        for previous in history:
            read_snapshot(previous)
    read_snapshot(snapshot)
    return roots, resources


def recorded_resources(connection, state_dir, repo, plan_id, canceled):
    table_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone()
    original = connection.execute('SELECT * FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone() if table_exists else None
    original = dict(original) if original else None
    try:
        checkpoint = original
        if canceled is not None and 'recovery_evidence' in canceled:
            checkpoint = json.loads(canceled['recovery_evidence']).get('checkpoints', {}).get('execution_checkpoints')
        snapshot = json.loads(checkpoint['snapshot']) if checkpoint and checkpoint['snapshot'] is not None else {}
        roots, resources = document_resources(snapshot, state_dir, repo, plan_id)
    except (KeyError, TypeError, ValueError, RuntimeError, RecursionError) as error:
        raise CallDeferred('전용 문서 사본의 저장 소유권을 확인할 수 없습니다.') from error

    return original, roots, resources


def removal_recoverable(connection, state_dir, repo, plan_id, canceled, intent):
    if intent.get('owner') != 'plan:' + plan_id or intent.get('kind') != 'managed_worktree_remove':
        return False
    try:
        _checkpoint, _roots, resources = recorded_resources(connection, state_dir, repo, plan_id, canceled)
        return intent.get('target') in resources
    except CallDeferred:
        return False


@contextmanager
def cleaned_documents(connection, state_dir, repo_path, repo, plan_id, canceled, *, git,
                      assess_cleanup, remove_cleanup_worktree, recover_cleanup_worktree, check_owner):
    """Keep copy leases and the original checkpoint CAS until final DB erasure."""
    table_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone()
    original, roots, resources = recorded_resources(connection, state_dir, repo, plan_id, canceled)

    def check_checkpoint():
        current = connection.execute('SELECT * FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone() if table_exists else None
        if (dict(current) if current else None) != original:
            raise CallDeferred('문서 사본 정리 중 checkpoint가 변경됐습니다.')

    def check():
        check_checkpoint()
        if any(root.exists() or root.is_symlink() for root in roots):
            raise CallDeferred('전용 문서 사본이 다시 생성돼 최종 제거를 보류합니다.')

    def check_copy_owner(raw):
        workspace = Path(raw)
        if workspace.is_symlink():
            raise CallDeferred('전용 문서 사본의 symlink 소유권을 확인할 수 없습니다.')
        tables = ['exec_plans', 'projects']
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_groups'").fetchone():
            tables.append('manual_work_groups')
        # ponytail: O(copies * stored workspaces) handles path aliases without a new index.
        # Index canonical resource owners if this scan becomes material.
        for table in tables:
            for row in connection.execute('SELECT worktree FROM ' + table + ' WHERE worktree IS NOT NULL'):
                other = Path(row['worktree'])
                if (not other.is_absolute() or workspace.resolve().is_relative_to(other.resolve())
                        or other.resolve().is_relative_to(workspace.resolve())):
                    raise CallDeferred('독립 실행·프로젝트·수동 작업이 참조하는 문서 사본을 보존합니다.')
        from loop_manager_internal.storage import runtime_state
        for project in connection.execute('SELECT planning_preserved_state FROM projects WHERE planning_preserved_state IS NOT NULL'):
            try:
                preserved = runtime_state._preserved_planning_values(project['planning_preserved_state'])['worktree']
            except (runtime_state.RuntimeStateError, TypeError, ValueError) as error:
                raise CallDeferred('보존된 planning 문맥의 사본 소유권을 확인할 수 없습니다.') from error
            if preserved is not None and (not Path(preserved).is_absolute()
                    or Path(preserved).resolve() == workspace.resolve()):
                raise CallDeferred('보존된 planning 문맥이 사용하는 문서 사본을 보존합니다.')

    locks = [prefix + path for path in resources for prefix in ('worktree:', 'git:')]
    locks = [key for key in locks if not resource_owned(Path(state_dir), repo, key)]
    try:
        with resource_locks(Path(state_dir), repo, locks, owner='workspace-reservation-recovery'):
            check_owner()
            check_checkpoint()
            for root in roots:
                if any(parent.is_symlink() for parent in (root, *root.parents)):
                    raise CallDeferred('전용 문서 사본의 symlink 소유권을 확인할 수 없습니다.')
                if root.exists() and (not root.is_dir() or any(str(child) not in resources for child in root.iterdir())):
                    raise CallDeferred('저장 기록에 없는 전용 문서 사본을 보존합니다.')
            from loop_manager_internal.git.manager_git_journal import has_pending_project_mutation
            for raw, head in resources.items():
                workspace = Path(raw)
                check_owner()
                check_checkpoint()
                check_copy_owner(raw)
                if has_pending_project_mutation(Path(state_dir), repo, targets={raw}):
                    allowed = lambda intent: (intent.get('owner') == 'plan:' + plan_id
                        and intent.get('kind') == 'managed_worktree_remove' and intent.get('target') == raw)
                    if (recover_cleanup_worktree is None or has_pending_project_mutation(
                            Path(state_dir), repo, targets={raw}, recoverable=allowed)):
                        raise CallDeferred('전용 문서 사본의 다른 미확정 Git 작업을 보존합니다.')
                    recover_cleanup_worktree(Path(state_dir), repo_path, workspace)
                    if has_pending_project_mutation(Path(state_dir), repo, targets={raw}) and (
                            not workspace.exists() or has_pending_project_mutation(
                                Path(state_dir), repo, targets={raw}, recoverable=allowed)):
                        raise CallDeferred('전용 문서 worktree 제거 journal의 복구가 미완료입니다.')
                    # A native no-effect intent remains active until execute()
                    # proves its original state, aborts it and starts this removal.
                registration = git(repo_path, 'worktree', 'list', '--porcelain')
                blocks = [block.splitlines() for block in registration.stdout.split('\n\n') if 'worktree ' + raw in block.splitlines()]
                if registration.returncode:
                    raise CallDeferred('전용 문서 worktree 등록을 확인할 수 없습니다.')
                if not workspace.exists():
                    if workspace.is_symlink() or blocks:
                        raise CallDeferred('사라진 문서 사본의 등록·소유권 정리가 남아 있습니다.')
                    continue
                check_copy_owner(raw)
                if (registration.returncode or len(blocks) != 1 or 'detached' not in blocks[0]
                        or git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                        != git(repo_path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                        or assess_cleanup is None or remove_cleanup_worktree is None):
                    raise CallDeferred('전용 문서 worktree의 현재 등록·정리 권한을 확인할 수 없습니다.')
                check_owner()
                check_checkpoint()
                check_copy_owner(raw)
                assessment = assess_cleanup(workspace, head)
                if assessment.status != 'safe':
                    raise CallDeferred('변경되거나 미확인인 전용 문서 worktree를 보존합니다.')
                check_owner()
                check_checkpoint()
                check_copy_owner(raw)
                removed = remove_cleanup_worktree(repo_path, workspace, assessment, state_dir=Path(state_dir), owner='plan:' + plan_id)
                if removed.status != 'safe' or workspace.exists():
                    raise CallDeferred('전용 문서 worktree 정리가 미완료입니다.')
                registration = git(repo_path, 'worktree', 'list', '--porcelain')
                if registration.returncode or 'worktree ' + raw in registration.stdout.splitlines():
                    raise CallDeferred('전용 문서 worktree의 등록 해제가 미확정입니다.')
                if has_pending_project_mutation(Path(state_dir), repo, targets={raw}):
                    raise CallDeferred('전용 문서 worktree 제거의 journal 확정이 남아 있습니다.')
            for root in roots:
                if root.exists():
                    root.rmdir()
            check()
            yield SimpleNamespace(check=check, worktrees=tuple(resources))
    except (OSError, RuntimeError) as error:
        if isinstance(error, CallDeferred):
            raise
        raise CallDeferred('전용 문서 사본의 안전 정리가 미확정입니다.') from error
