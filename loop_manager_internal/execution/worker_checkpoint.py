"""A stopped Worker's bounded request, executed through Manager-owned effects."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.recovery.checkpoint_ownership import committed_tree_matches_intent
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.storage import runtime_state


def _contents(worktree, paths):
    result = {}
    root = worktree.resolve()
    for path in paths:
        target = root / path
        if target.parent.resolve() != target.parent or Path(path).is_absolute() or '..' in Path(path).parts:
            raise PublicationSafetyError('checkpoint 후보 경로가 worktree 밖입니다.')
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            result[path] = None
            continue
        if stat.S_ISLNK(mode):
            data = os.readlink(target).encode('utf-8')
        elif stat.S_ISREG(mode):
            data = target.read_bytes()
        else:
            raise PublicationSafetyError('checkpoint 후보는 파일 또는 symlink여야 합니다.')
        result[path] = (stat.S_IMODE(mode), hashlib.sha256(data).hexdigest())
    # JSON uses lists; the persisted and freshly observed shape must be identical.
    return {key: list(value) if value is not None else None for key, value in result.items()}


def stage_candidate(ports, context):
    """Stage the validated final candidate through explicit role-owned paths."""
    snapshot = ports.capture_git_snapshot(context.worktree)
    if snapshot.branch != context.branch or controlling_git_operation(snapshot.operations):
        raise PublicationSafetyError('최종 checkpoint의 branch·Git operation을 확인할 수 없습니다.')
    if any(agent_scope_decision.index_security_flags(entries)
           or any(entry.split()[-1:] != ['0'] for entry in entries)
           for entries in snapshot.index.values()):
        raise PublicationSafetyError('최종 checkpoint index flags·stage가 유효하지 않습니다.')
    allow_plans = ports._published_plan_allows_common_plans(ports.identity.repo_path, ports.snapshot().catalog_plan)
    paths = sorted(snapshot.dirty)
    if any(not agent_scope_decision._worker_path_allowed(path, context.plan.path,
            allow_common_plans=allow_plans) for path in paths):
        raise PublicationSafetyError('최종 checkpoint에 Worker 범위 밖 변경을 섞을 수 없습니다.')
    if paths:
        ports.git(context.worktree, '--literal-pathspecs', 'add', '--', *paths)


def save_request(ports, context, outcome, message, before, *, delivery=None, session=None, invocation_id=None):
    if context.resolving_rebase or ports.checkpoint is None:
        raise PublicationSafetyError('진행 중 rebase에서는 checkpoint를 요청할 수 없습니다.')
    if outcome.plan_id != context.plan.plan_id or outcome.start_head != before.head:
        raise PublicationSafetyError('checkpoint 요청이 현재 작업·호출 시작 HEAD와 다릅니다.')
    after = ports.capture_git_snapshot(context.worktree)
    if after.head != before.head or after.branch != context.branch or controlling_git_operation(after.operations):
        raise PublicationSafetyError('Worker가 직접 HEAD를 변경했거나 Git operation이 남아 있습니다.')
    baseline = before
    row = ports._row()
    if row['agent_snapshot']:
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        original = GitSnapshot.deserialize(str(row['agent_snapshot']))
        if (original.head == before.head and original.branch == before.branch
                and original.scope_id == before.scope_id):
            baseline = original  # Include unfinished work from this owned scope.
    paths = sorted(path for path in after.dirty.keys() | baseline.dirty.keys()
                   if after.dirty.get(path) != baseline.dirty.get(path)
                   or after.index.get(path) != baseline.index.get(path))
    paths = sorted(set(paths) | {path for path in after.index.keys() | baseline.index.keys()
                                if after.index.get(path) != baseline.index.get(path)})
    if not paths:
        raise PublicationSafetyError('checkpoint 요청에 이번 호출의 변경 파일이 없습니다.')
    allow_plans = ports._published_plan_allows_common_plans(ports.identity.repo_path, ports.snapshot().catalog_plan)
    if any(not agent_scope_decision._worker_path_allowed(path, context.plan.path,
            allow_common_plans=allow_plans) for path in paths):
        raise PublicationSafetyError('checkpoint 요청에 허용 범위 밖 파일이 있습니다.')
    if any(agent_scope_decision.index_security_flags(entries) for entries in after.index.values()):
        raise PublicationSafetyError('checkpoint index flags가 유효하지 않습니다.')
    staged = set(filter(None, ports.git(context.worktree, 'diff', '--cached', '--name-only',
                                         '--no-renames', '-z', '--').stdout.split('\0')))
    if staged - set(paths):
        raise PublicationSafetyError('checkpoint 요청 밖의 staged 파일을 보존합니다.')
    request = {'repo': ports.identity.repo, 'plan_id': context.plan.plan_id,
               'branch': context.branch, 'worktree': str(context.worktree),
               'start_head': before.head, 'message': message, 'title': outcome.commit_title,
               'files': _contents(context.worktree, paths), 'session': session,
               'delivery': delivery.attempt_id if delivery is not None else None,
               'invocation_id': invocation_id, 'code': 0, 'tree': None}
    with runtime_state._transaction(ports.state.connection):
        ports.state.check_context(context)
        current = ports.checkpoint.get(ports.identity.repo, context.plan.plan_id)
        if current.operation is not None or (current.snapshot or {}).get('worker_checkpoint_request'):
            raise PublicationSafetyError('checkpoint 요청 전에 이전 Manager 효과를 끝내야 합니다.')
        ports.checkpoint.record(ports.identity.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=None,
            expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'worker_checkpoint_request': request})
    return resume_request(ports, context)


def resume_request(ports, context):
    """Finish exactly this request before another model call, including restart."""
    repo, plan_id = ports.identity.repo, context.plan.plan_id
    current = ports.checkpoint.get(repo, plan_id)
    request = (current.snapshot or {}).get('worker_checkpoint_request')
    if not request:
        return None
    fields = {'repo', 'plan_id', 'branch', 'worktree', 'start_head', 'message', 'title',
              'files', 'session', 'delivery', 'invocation_id', 'code', 'tree'}
    if (not isinstance(request, dict) or set(request) != fields or request.get('code') != 0
            or not isinstance(request.get('start_head'), str)
            or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', request['start_head']) is None
            or not isinstance(request.get('title'), str) or not request['title'].strip()
            or len(request['title']) > 200 or any(ord(c) < 32 for c in request['title'])
            or not isinstance(request.get('files'), dict) or not request['files']
            or any(not isinstance(path, str) or not path for path in request['files'])):
        raise PublicationSafetyError('저장된 checkpoint 요청 형식이 유효하지 않습니다.')
    ports.state.check_context(context)
    if any(request.get(key) != value for key, value in {
            'repo': repo, 'plan_id': plan_id, 'branch': context.branch,
            'worktree': str(context.worktree), 'session': ports._row()['worker_session_id']}.items()):
        raise PublicationSafetyError('저장된 checkpoint 요청의 작업·session identity가 다릅니다.')
    ports.validate_execplan_workspace_identity(ports.identity.state_dir, repo, plan_id,
                                               context.branch, context.worktree)
    ports.validate_managed_worktree(ports.identity.repo_path, context.worktree, context.branch)
    invocation_id = request.get('invocation_id')
    if invocation_id:
        row = ports.state.connection.execute(
            'SELECT * FROM agent_invocations WHERE invocation_id=?', (invocation_id,)).fetchone()
        if (row is None or row['repo'] != repo or row['plan_id'] != plan_id or row['role'] != 'worker'
                or row['execution_status'] != 'executed' or row['result_code'] not in (None, 0)):
            raise PublicationSafetyError('checkpoint 요청의 실제 Worker 종료 결과가 유효하지 않습니다.')
        if not row['execution_finished_at']:
            from loop_manager_internal.agents.process_receipt import recover
            if not row['process_receipt_path'] or recover(row['process_receipt_path'], ports.identity.state_dir) is None:
                raise PublicationSafetyError('checkpoint 요청의 Worker writer 종료가 확인되지 않았습니다.')
    head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
    if head == request['start_head']:
        if current.operation not in {None, 'commit'}:
            raise PublicationSafetyError('checkpoint 요청의 Git operation이 미확정입니다.')
        if _contents(context.worktree, request['files']) != request['files']:
            raise PublicationSafetyError('저장된 checkpoint 요청의 후보 파일이 변경됐습니다.')
        snapshot = ports.capture_git_snapshot(context.worktree)
        if snapshot.branch != context.branch or controlling_git_operation(snapshot.operations):
            raise PublicationSafetyError('checkpoint 요청의 branch·종료 상태가 변경됐습니다.')
        if any(agent_scope_decision.index_security_flags(entries) for entries in snapshot.index.values()):
            raise PublicationSafetyError('checkpoint index flags가 변경됐습니다.')
        staged = set(filter(None, ports.git(context.worktree, 'diff', '--cached', '--name-only',
                                             '--no-renames', '-z', '--').stdout.split('\0')))
        if staged - request['files'].keys():
            raise PublicationSafetyError('checkpoint 요청 밖 staged 파일을 보존합니다.')
        ports.git(context.worktree, '--literal-pathspecs', 'add', '--', *request['files'])
        if _contents(context.worktree, request['files']) != request['files']:
            raise PublicationSafetyError('checkpoint staging 중 후보 파일이 변경됐습니다.')
        tree = ports.git(context.worktree, 'write-tree').stdout.strip()
        if request['tree'] is not None and request['tree'] != tree:
            raise PublicationSafetyError('checkpoint 요청의 확정된 index tree가 변경됐습니다.')
        if request['tree'] is None:
            request = {**request, 'tree': tree}
            current = ports.checkpoint.record(repo, plan_id, expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'worker_checkpoint_request': request})
        ports._commit_owned(context, request['title'], expected_tree=request['tree'])
        current = ports.checkpoint.get(repo, plan_id)
        head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
    if (request['tree'] != (current.snapshot or {}).get('commit_tree_oid')
            or request['title'] != (current.snapshot or {}).get('commit_message')):
        raise PublicationSafetyError('checkpoint 요청과 Manager commit 후보가 다릅니다.')
    if not committed_tree_matches_intent(context.worktree, request['start_head'], head,
                                          current.snapshot or {}, ports.git):
        raise PublicationSafetyError('checkpoint 실제 parent/tree/message가 Manager intent와 다릅니다.')
    # _commit_owned's durable intent is also sufficient when commit succeeded but
    # its completion write was interrupted. It never creates a second commit.
    if current.operation == 'commit':
        current = ports.checkpoint.record(repo, plan_id, expected_generation=current.generation,
            operation=None, expected_head=head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**current.snapshot, 'after_head': head})
    if ports.publish_worker_checkpoints is None:
        raise PublicationSafetyError('checkpoint 게시 경로가 없습니다.')
    if current.operation == 'rebase_publish':
        if getattr(ports, '_publish_rebased_checkpoint', None) is None:
            raise PublicationSafetyError('checkpoint rebase 게시 경로가 없습니다.')
        ports._publish_rebased_checkpoint(context, candidate_head=head)
    ports._fetch_latest_base()
    published = ports.publish_worker_checkpoints(context, worker_start_head=request['start_head'],
        allowed_requirement_archives=ports.snapshot().archive_scope_sources)
    from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
    if isinstance(published, ExecutionOutcome):
        return published
    current = ports.checkpoint.get(repo, plan_id)
    if (current.snapshot or {}).get('worker_checkpoint_pending') or current.operation is not None:
        raise PublicationSafetyError('checkpoint 게시 완료 확인을 기다립니다.')
    if ports._row()['publication_remote_oid'] != head and not (published or {}).get('candidate_changed'):
        from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
        if (not valid_acceptance((current.snapshot or {}).get('current_rebase_acceptance'))
                or getattr(ports, '_publish_rebased_checkpoint', None) is None):
            raise PublicationSafetyError('checkpoint 원격 게시 완료가 확인되지 않았습니다.')
        ports._publish_rebased_checkpoint(context, candidate_head=head)
        current = ports.checkpoint.get(repo, plan_id)
        if (current.operation is not None or ports._row()['publication_remote_oid'] != head
                or current.observed_remote_oid != head):
            raise PublicationSafetyError('checkpoint 원격 게시 완료가 확인되지 않았습니다.')
    with runtime_state._transaction(ports.state.connection):
        ports.state.check()
        saved = dict(current.snapshot or {})
        saved.pop('worker_checkpoint_request', None)
        saved['worker_checkpoint_result'] = {'head': head, 'title': request['title']}
        saved['worker_checkpoint_requests'] = [*saved.get('worker_checkpoint_requests', []),
                                               {**request, 'head': head}]
        ports.checkpoint.record(repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot=saved)
        if request['delivery'] and request['session'] and ports.feedback:
            ports.feedback.repository.delivered(request['delivery'], request['session'])
        # Keep existing failures and audit. A checkpoint does not create a
        # completion result, Worker token, targeted PASS, verify or Review token.
        ports.state.update_project(repo, preserve_transaction=True, phase='WORKING',
            worker_result=None, worker_input_token=None, validated_base_sha=None,
            verify_token=None, review_token=None, publication_token=None,
            agent_role=None, agent_snapshot=None, agent_baseline=None, agent_recovery=None,
            agent_input_plan=None, agent_result_plan=None, agent_input_fingerprint=None,
            agent_result_snapshot=None, agent_manager_snapshot=None)
    return ports._retry('Manager checkpoint 게시를 확인했습니다. 미완료 작업을 이어갑니다.')
