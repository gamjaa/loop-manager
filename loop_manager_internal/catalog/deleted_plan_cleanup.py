"""Finish explicit remote task withdrawal before ordinary catalog recovery."""
from __future__ import annotations

import json
import hashlib
import shutil
from types import SimpleNamespace
from pathlib import Path
from contextlib import contextmanager
import re

from . import execplan_catalog as catalog
from loop_manager_internal.recovery.plan_recovery import reserved_workspace_ownership
from loop_manager_internal.storage import deleted_plan_state, local_recovery_state, runtime_state
from loop_manager_internal.storage.task_calls import CallDeferred


def resolved_references(plan_id, references, documents):
    """A current declaration must retire the goal, not merely drop its edge."""
    affected = {issue.plan_id for issue in references if issue.source and f'[{plan_id}] ' in issue.source}
    result = {}
    for key in affected - {plan_id}:
        entry = documents.get(key)
        if entry is None:
            return None
        plan = catalog.parse_execplan_text(entry.path, entry.text, validate_sources=False)
        if plan_id in plan['depends_on']:
            return None
        decisions = []
        for line in entry.text.partition('## Decision Log')[2].splitlines():
            if line.startswith('DEPENDENCY_RECOVERY:'):
                try:
                    item = json.loads(line.partition(':')[2])
                except (ValueError, TypeError):
                    return None
                if not isinstance(item, dict):
                    return None
                if item.get('deleted_id') == plan_id:
                    decisions.append(item)
        if len(decisions) != 1:
            return None
        item = decisions[0]
        replacements = item.get('replacement_ids')
        sources = item.get('sources')
        if (not isinstance(item.get('reason'), str) or not item['reason'].strip()
                or not isinstance(sources, list) or not sources
                or not all(isinstance(source, str) and source in plan['sources'] for source in sources)
                or not isinstance(replacements, list)
                or not all(isinstance(replacement, str) for replacement in replacements)):
            return None
        if item.get('outcome') == 'obsolete' and not replacements:
            result[key] = plan['depends_on']
        elif (item.get('outcome') == 'replaced' and replacements
              and set(replacements) <= set(plan['depends_on'])
              and all(replacement in documents for replacement in replacements)):
            result[key] = plan['depends_on']
        else:
            return None
    return result


def _artifact_references(connection, state_dir):
    """Read exact stored owners; a shared session name alone grants no file scope."""
    def stored_path(raw):
        path = Path(raw)
        if not path.is_absolute():
            raise CallDeferred('공유 산출물 참조의 절대 경로를 확인할 수 없습니다.')
        return path

    references = []
    for call in connection.execute('SELECT repo,plan_id,process_receipt_path FROM agent_invocations'):
        if call['process_receipt_path']:
            receipt = stored_path(call['process_receipt_path'])
            owner = (call['repo'], call['plan_id'])
            references.append((receipt, owner))
            if receipt.name.endswith('.process.json'):
                references.append((receipt.with_name(receipt.name.removesuffix('.process.json')), owner))
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'targeted_test_collections' in tables:
        references.extend((stored_path(row['directory']), (row['repo'], row['plan_id'])) for row in connection.execute(
            'SELECT c.directory,a.repo,a.plan_id FROM targeted_test_collections c '
            'LEFT JOIN agent_invocations a ON a.invocation_id=c.invocation_id'))
    if 'task_call_reservations' in tables:
        references.extend((stored_path(row['receipt_path']), (row['repo'], row['plan_id'])) for row in connection.execute(
            'SELECT repo,plan_id,receipt_path FROM task_call_reservations WHERE receipt_path IS NOT NULL'))
    references.extend((Path(state_dir).resolve() / 'logs' / hashlib.sha256(row['worktree'].encode()).hexdigest()[:16],
                       (row['repo'], row['plan_id'])) for row in connection.execute(
                           'SELECT repo,plan_id,worktree FROM exec_plans WHERE worktree IS NOT NULL'))
    return [(candidate, owner) for path, owner in references for candidate in (path, path.resolve())]


def _independent_artifact(path, owner, references):
    candidate = path.resolve()
    return any(other != owner and (stored.is_relative_to(candidate) or candidate.is_relative_to(stored))
               for stored, other in references)


def remove_pending_files(connection, repo, state_dir):
    root = Path(state_dir).resolve()
    pending = set()
    for row in connection.execute('SELECT plan_id,path FROM deleted_plan_files WHERE repo=?', (repo,)).fetchall():
        path = Path(row['path'])
        allowed = any(path.resolve().is_relative_to(root / folder) and path.resolve() != root / folder
                      for folder in ('agent-runtime', 'logs'))
        if not allowed or path.is_symlink():
            raise ValueError('삭제할 작업 산출물 경로가 관리 범위 밖입니다.')
        # ponytail: O(queued paths * owners) keeps each destructive step fresh.
        # Index exact path/owner relations if queues grow; retain this recheck.
        try:
            shared = _independent_artifact(path, (repo, row['plan_id']), _artifact_references(connection, state_dir))
        except (OSError, RuntimeError, TypeError, ValueError):
            shared = True  # Unconfirmed references retain this queue item.
        if shared:
            if row['plan_id'] not in pending:
                from loop_manager_internal.runtime.operation_events import emit
                emit('삭제 산출물 정리 대기: 독립 또는 미확인 참조를 보존합니다.',
                     project=repo, plan=row['plan_id'], stage='DELETED_PLAN_CLEANUP', resource=str(path))
            pending.add(row['plan_id'])
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        with runtime_state._transaction(connection):
            connection.execute('DELETE FROM deleted_plan_files WHERE repo=? AND plan_id=? AND path=?',
                               (repo, row['plan_id'], row['path']))
    return pending


def artifact_paths(connection, repo, row, state_dir):
    paths = []
    owner = (repo, row['plan_id'])
    try:
        references = _artifact_references(connection, state_dir)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise CallDeferred('공유 산출물 경로의 현재 귀속을 확인할 수 없습니다.') from error
    calls = connection.execute('SELECT repo,plan_id,process_receipt_path FROM agent_invocations').fetchall()
    for call in calls:
        if call['repo'] != repo or call['plan_id'] != row['plan_id'] or not call['process_receipt_path']:
            continue
        source = Path(call['process_receipt_path'])
        if source.is_symlink() or source.parent.is_symlink():
            raise CallDeferred('호출 산출물의 symlink 소유권을 확인할 수 없습니다.')
        receipt = source.resolve()
        shared = _independent_artifact(receipt.parent, owner, references)
        if not shared:
            paths.append(receipt.parent)
        elif receipt.name.endswith('.process.json'):
            paths.extend((receipt, receipt.with_name(receipt.name.removesuffix('.process.json'))))
        else:
            raise CallDeferred('공유 폴더의 호출 산출물 귀속을 확인할 수 없습니다.')
    if row['worktree']:
        paths.append(Path(state_dir) / 'logs' / hashlib.sha256(row['worktree'].encode()).hexdigest()[:16])
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='targeted_test_collections'").fetchone():
        for collection in connection.execute('SELECT c.directory FROM targeted_test_collections c '
                'JOIN agent_invocations a ON a.invocation_id=c.invocation_id WHERE a.repo=? AND a.plan_id=?', owner):
            path = Path(collection['directory']).absolute()
            if not any(path.resolve().is_relative_to(parent.resolve()) for parent in paths):
                paths.append(path)
    if any(path.is_symlink() or _independent_artifact(path, owner, references) for path in paths):
        raise CallDeferred('독립 작업이 참조하는 공유 산출물은 제거할 수 없습니다.')
    return tuple(dict.fromkeys(paths))


def release_reference_holds(connection, repo, key, path, resolved):
    dependencies = {row['plan_id']: json.loads(row['depends_on']) for row in connection.execute(
        'SELECT plan_id,depends_on FROM exec_plans WHERE repo=?', (repo,))}
    for held in connection.execute('SELECT plan_id,evidence FROM execplan_catalog_holds WHERE repo=?', (repo,)).fetchall():
        evidence = json.loads(held['evidence'])
        causes = evidence.get('causes', ())
        removed = lambda cause: (cause.get('plan_id') == key or cause.get('source') == path
            or cause.get('code') == 'deleted-goal' and cause.get('message', '').endswith(' -> ' + key))
        if held['plan_id'] not in resolved and not any(removed(cause) for cause in causes):
            continue
        closure, pending = set(), [held['plan_id']]
        while pending:
            target = pending.pop()
            if target not in closure:
                closure.add(target)
                pending.extend(dependencies.get(target, ()))
        # Old holds stored the whole catalog's causes, including unrelated tasks.
        remaining = [cause for cause in causes if not removed(cause)
                     and (not cause.get('plan_id') or cause['plan_id'] in closure)]
        if remaining:
            evidence['causes'] = remaining
            connection.execute('UPDATE execplan_catalog_holds SET evidence=? WHERE repo=? AND plan_id=?',
                               (json.dumps(evidence, ensure_ascii=False), repo, held['plan_id']))
        else:
            connection.execute('DELETE FROM execplan_catalog_holds WHERE repo=? AND plan_id=?', (repo, held['plan_id']))


def _unowned_cancellations(connection, repo):
    """Read complete READY audits that retain no resource ownership."""
    from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_cancellations'").fetchone():
        return
    for canceled in connection.execute('SELECT * FROM execplan_cancellations WHERE repo=?', (repo,)).fetchall():
        if connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?', (repo, canceled['plan_id'])).fetchone():
            continue
        try:
            audit = json.loads(canceled['recovery_evidence'] or '{}')
            saved = audit['row']
            checkpoints = audit.get('checkpoints')
            if checkpoints is not None and not isinstance(checkpoints, dict):
                continue
            if checkpoints:
                if not isinstance(checkpoints, dict) or set(checkpoints) != {'execution_checkpoints'}:
                    continue
                checkpoint = checkpoints['execution_checkpoints']
                if (not isinstance(checkpoint, dict) or set(checkpoint) != {
                        'repo', 'plan_id', 'generation', 'operation', 'expected_head', 'target_base_oid',
                        'observed_remote_oid', 'snapshot', 'workspace_ownership_held'}
                        or checkpoint['repo'] != canceled['repo'] or checkpoint['plan_id'] != canceled['plan_id']
                        or type(checkpoint['generation']) is not int or checkpoint['generation'] < 0
                        or type(checkpoint['workspace_ownership_held']) is not int or checkpoint['workspace_ownership_held'] != 0
                        or any(checkpoint[field] is not None for field in (
                            'operation', 'expected_head', 'target_base_oid', 'observed_remote_oid'))
                        or (checkpoint['snapshot'] is not None and json.loads(checkpoint['snapshot']) != {})):
                    continue
            started = isinstance(saved, dict) and saved.get('ever_started') == 1
            historical_fields = {
                'active_sha', 'validated_base_sha', 'last_error', 'blocked_fingerprint',
                'blocked_worktree_fingerprint', 'blocked_review_fingerprint', 'worker_session_id',
                'reviewer_session_id', 'worker_result', 'verify_result', 'review_result',
                'blocked_reason', 'guidance_plan_hash',
            } if started else set()
            judgment = audit.get('judgment')
            decision = judgment.get('decision') if isinstance(judgment, dict) else None
            decision = decision if isinstance(decision, dict) else {}
            if started and (audit.get('cleanup_pending') is not False
                    or decision.get('id') != saved['plan_id'] or decision.get('outcome') != 'withdrawn'
                    or decision.get('source') not in json.loads(saved['sources'])
                    or not isinstance(decision.get('reason'), str) or not decision['reason'].strip()):
                continue
            if started and not connection.execute(
                    "SELECT 1 FROM agent_invocations WHERE repo=? AND plan_id=? "
                    "AND execution_status='executed' AND execution_started_at IS NOT NULL",
                    (repo, canceled['plan_id'])).fetchone():
                continue  # Missing historical calls cannot prove a started writer's termination.
            if (not isinstance(saved, dict) or saved['status'] != 'READY'
                    or type(saved['ever_started']) is not int or saved['ever_started'] not in (0, 1)
                    or type(saved['workspace_owned']) is not int or saved['workspace_owned'] != 0
                    or type(saved['retry_count']) is not int or saved['retry_count'] < 0
                    or (not started and saved['retry_count'] != 0) or saved['pr_conflict_recovery'] != 0
                    or any(not isinstance(json.loads(saved[field]), dict) or not started and saved[field] != '{}'
                           for field in ('verify_failure_history', 'review_failure_history', 'review_level_history'))
                    or any(saved[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS if field not in historical_fields)
                    or any(saved[field] is not None for field in ('pr_conflict_head_oid', 'pr_conflict_base_oid'))
                    or audit.get('cleanup_pending')
                    or any(saved[field] != canceled[field] for field in (
                        'repo', 'plan_id', 'path', 'title', 'sources', 'depends_on', 'priority', 'content_hash'))
                    or (saved['current_revision_sha'] or saved['plan_sha']) != canceled['plan_sha']
                    or not all(re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', canceled[field] or '')
                               for field in ('plan_sha', 'deletion_commit'))
                    or catalog.plan_content_hash(audit['text']) != canceled['content_hash']):
                continue
            parsed = catalog.parse_execplan_text(saved['path'], audit['text'], validate_sources=False)
            if (parsed['id'] != saved['plan_id'] or parsed['title'] != saved['title']
                    or parsed['sources'] != json.loads(saved['sources'])
                    or parsed['depends_on'] != json.loads(saved['depends_on'])
                    or any(parsed[field] != canceled[field] for field in ('priority', 'complexity', 'risk'))):
                continue
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue
        yield saved, dict(canceled)


@contextmanager
def _rowless_ownership(connection, state_dir, repo, row, canceled):
    from loop_manager_internal.runtime.resource_lock import resource_locks
    from loop_manager_internal.storage import task_calls, work_claims
    from loop_manager_internal.maintenance.maintenance_validation import _plan_id_from_path
    key = row['plan_id']
    with resource_locks(Path(state_dir), repo, ['plan:' + key], owner='deleted-plan-cleanup'):
        if connection.in_transaction:
            raise CallDeferred('row 없는 삭제 대상의 외부 관측은 DB transaction 밖에서 수행해야 합니다.')
        def check():
            current = connection.execute('SELECT * FROM execplan_cancellations WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
            project = connection.execute('SELECT plan_path FROM projects WHERE repo=?', (repo,)).fetchone()
            if (current is None or dict(current) != canceled
                    or project is not None and _plan_id_from_path(project['plan_path'] or '') == key
                    or connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
                    or task_calls.pending_for_plan(connection, repo, key)):
                raise CallDeferred('row 없는 삭제 대상의 감사·writer 상태가 변경됐습니다.')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone() and connection.execute(
                    'SELECT 1 FROM execution_checkpoints WHERE repo=? AND plan_id=? AND operation IS NOT NULL', (repo, key)).fetchone():
                raise CallDeferred('row 없는 삭제 대상의 미확정 Git operation이 남아 있습니다.')
            try:
                _state, owned = work_claims.manual_plan_ownership(connection, repo, key, row)
                ids, paths = work_claims.publication_claims(connection, repo)
                if owned or key in ids or {row['path'], *json.loads(row['sources'])} & paths:
                    raise CallDeferred('row 없는 삭제 대상의 수동·게시 소유권이 남아 있습니다.')
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise CallDeferred('row 없는 삭제 대상의 현재 소유권을 확인할 수 없습니다.') from error
        check()
        yield SimpleNamespace(check=check)


def cleanup(connection, repo_path, repo, pinned, references, *, git, git_bytes, pr_for_branch, state_dir, revision_is_current,
            assess_cleanup=None, remove_cleanup_worktree=None):
    """Observe outside the DB writer; recheck under task resource leases before erasure."""
    remove_pending_files(connection, repo, state_dir)
    if pinned.regular_paths is None or pr_for_branch is None:
        return ()
    deleted = []
    rows = [(row, None) for row in connection.execute("SELECT * FROM exec_plans WHERE repo=? AND status!='MERGED' AND agent_role IS NULL", (repo,)).fetchall()]
    rows.extend(_unowned_cancellations(connection, repo))
    for row, canceled in rows:
        key, path = row['plan_id'], row['path']
        if path in pinned.regular_paths or key in pinned.snapshot.observed_ids:
            continue
        revision = row['current_revision_sha'] or row['plan_sha']
        if not revision:
            continue
        if git(repo_path, 'merge-base', '--is-ancestor', revision, pinned.revision_sha, check=False).returncode:
            continue
        if canceled is not None and git(repo_path, 'merge-base', '--is-ancestor', canceled['deletion_commit'], pinned.revision_sha, check=False).returncode:
            continue
        contents = git_bytes(repo_path, 'show', f'{revision}:{path}')
        if catalog.plan_content_hash(contents) != row['content_hash']:
            continue
        documents = dict(pinned.snapshot.all)
        # Withheld declarations remain available for goal recovery, never execution.
        for held_key, held_path, _hash in pinned.snapshot.withheld:
            text = git_bytes(repo_path, 'show', f'{pinned.revision_sha}:{held_path}').decode('utf-8')
            documents[held_key] = SimpleNamespace(path=held_path, text=text)
        resolved = resolved_references(key, references, documents)
        if resolved is None:
            continue
        if any(key in json.loads(other['depends_on']) and other['plan_id'] not in resolved
               for other in connection.execute('SELECT plan_id,depends_on FROM exec_plans WHERE repo=? AND plan_id<>?', (repo, key))):
            continue
        if row['branch'] and row['branch'] != 'loop-manager/' + key:
            continue
        try:
            lease = (reserved_workspace_ownership(connection, state_dir, repo, key) if canceled is None
                     else _rowless_ownership(connection, state_dir, repo, row, canceled))
            with lease as ownership:
                if not revision_is_current():
                    continue
                if canceled is not None:
                    canonical = 'loop-manager/' + key
                    try:
                        remote = git(repo_path, 'ls-remote', '--heads', 'origin', 'refs/heads/' + canonical, check=False)
                        local = git(repo_path, 'rev-parse', '--verify', 'refs/heads/' + canonical, check=False)
                        pr = pr_for_branch(repo_path, canonical)
                    except (OSError, RuntimeError) as error:
                        raise CallDeferred('row 없는 삭제 대상의 실제 PR·ref를 확인할 수 없습니다.') from error
                    if (pr is not None or remote.returncode or remote.stdout.strip() or local.returncode not in (1, 128)):
                        continue
                pr = pr_for_branch(repo_path, row['branch'], row['pr_url']) if row['branch'] else None
                if row['pr_url'] and (pr is None or pr.get('url') != row['pr_url']):
                    continue
                if pr is not None and (pr.get('state') != 'CLOSED' or pr.get('mergedAt')
                        or pr.get('headRefName') != row['branch']):
                    continue
                ownership.check()
                if canceled is None and dict(connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()) != dict(row):
                    continue
                from loop_manager_internal.git.manager_git_journal import has_pending_project_mutation
                if has_pending_project_mutation(Path(state_dir), repo, targets={row['worktree'], row['branch']}):
                    continue
                if canceled is not None and has_pending_project_mutation(Path(state_dir), repo,
                        recoverable=lambda intent: intent['owner'] != 'plan:' + key):
                    continue
                # shortcut: surviving remote refs stay pending until lease-aware deletion is available.
                if row['branch'] and git(repo_path, 'ls-remote', '--heads', 'origin', f"refs/heads/{row['branch']}").stdout.strip():
                    continue
                if row['branch']:
                    registrations = git(repo_path, 'worktree', 'list', '--porcelain').stdout.split('\n\n')
                    if any(f"branch refs/heads/{row['branch']}" in block.splitlines()
                           and f"worktree {row['worktree']}" not in block.splitlines() for block in registrations):
                        continue
                if row['worktree']:
                    workspace = Path(row['worktree'])
                    managed = Path(state_dir).resolve() / 'worktrees'
                    if (workspace.is_symlink() or workspace.name != key
                            or workspace.resolve().parent.parent != managed):
                        continue
                    if workspace.exists():
                        # Check the actual registration before deleting the exact owned workspace.
                        if git(workspace, 'symbolic-ref', '--quiet', 'HEAD').stdout.strip() != f"refs/heads/{row['branch']}":
                            continue
                        if (git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                                != git(repo_path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()):
                            continue
                        ownership.check()
                        if assess_cleanup is None or remove_cleanup_worktree is None:
                            continue
                        head = pr.get('headRefOid') if pr else row['publication_remote_oid']
                        assessment = assess_cleanup(workspace, head)
                        if assessment.status != 'safe':
                            continue
                        removed = remove_cleanup_worktree(repo_path, workspace, assessment,
                            state_dir=Path(state_dir), owner='plan:' + key)
                        if removed.status != 'safe' or workspace.exists():
                            continue
                if row['branch']:
                    ref = f"refs/heads/{row['branch']}"
                    local = git(repo_path, 'rev-parse', '--verify', ref, check=False)
                    if local.returncode == 0:
                        ownership.check()
                        git(repo_path, 'update-ref', '-d', ref, local.stdout.strip())
                if not revision_is_current():
                    continue
                ownership.check()
                artifacts = artifact_paths(connection, repo, row, state_dir)
                with runtime_state._transaction(connection):
                    ownership.check()
                    for other, dependencies in resolved.items():
                        connection.execute('UPDATE exec_plans SET depends_on=? WHERE repo=? AND plan_id=?',
                                           (json.dumps(dependencies, ensure_ascii=False, separators=(',', ':')), repo, other))
                    deleted_plan_state.forget(connection, repo, key, artifacts=artifacts)
                    release_reference_holds(connection, repo, key, path, resolved)
                if key not in remove_pending_files(connection, repo, state_dir):
                    deleted.append(key)
        except (CallDeferred, local_recovery_state.LocalRecoveryRequestError):
            continue
    return tuple(deleted)
