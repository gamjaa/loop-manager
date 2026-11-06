"""Finish explicit remote task withdrawal before ordinary catalog recovery."""
from __future__ import annotations

import json
import hashlib
import shutil
from types import SimpleNamespace
from pathlib import Path

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


def cleanup(connection, repo_path, repo, pinned, references, *, git, git_bytes, pr_for_branch, state_dir, revision_is_current,
            assess_cleanup=None, remove_cleanup_worktree=None):
    """Observe outside the DB writer; recheck under task resource leases before erasure."""
    remove_pending_files(connection, repo, state_dir)
    if pinned.regular_paths is None or pr_for_branch is None:
        return ()
    deleted = []
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND status!='MERGED' AND agent_role IS NULL", (repo,)).fetchall()
    for row in rows:
        key, path = row['plan_id'], row['path']
        if path in pinned.regular_paths or key in pinned.snapshot.observed_ids:
            continue
        revision = row['current_revision_sha'] or row['plan_sha']
        if not revision:
            continue
        if git(repo_path, 'merge-base', '--is-ancestor', revision, pinned.revision_sha, check=False).returncode:
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
            with reserved_workspace_ownership(connection, state_dir, repo, key) as ownership:
                if not revision_is_current():
                    continue
                pr = pr_for_branch(repo_path, row['branch'], row['pr_url']) if row['branch'] else None
                if row['pr_url'] and (pr is None or pr.get('url') != row['pr_url']):
                    continue
                if pr is not None and (pr.get('state') != 'CLOSED' or pr.get('mergedAt')
                        or pr.get('headRefName') != row['branch']):
                    continue
                if dict(connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()) != dict(row):
                    continue
                from loop_manager_internal.git.manager_git_journal import has_pending_project_mutation
                if has_pending_project_mutation(Path(state_dir), repo, targets={row['worktree'], row['branch']}):
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
                with runtime_state._transaction(connection):
                    ownership.check()
                    for other, dependencies in resolved.items():
                        connection.execute('UPDATE exec_plans SET depends_on=? WHERE repo=? AND plan_id=?',
                                           (json.dumps(dependencies, ensure_ascii=False, separators=(',', ':')), repo, other))
                    deleted_plan_state.forget(connection, repo, key,
                        artifacts=artifact_paths(connection, repo, row, state_dir))
                    release_reference_holds(connection, repo, key, path, resolved)
                if key not in remove_pending_files(connection, repo, state_dir):
                    deleted.append(key)
        except (CallDeferred, local_recovery_state.LocalRecoveryRequestError):
            continue
    return tuple(deleted)
