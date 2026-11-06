"""Finish explicit remote task withdrawal before ordinary catalog recovery."""
from __future__ import annotations

import json
import hashlib
import shutil
from types import SimpleNamespace
from pathlib import Path
from contextlib import contextmanager, ExitStack
import re

from . import execplan_catalog as catalog
from . import deleted_document_cleanup
from loop_manager_internal.recovery.plan_recovery import reserved_workspace_ownership
from loop_manager_internal.storage import deleted_plan_state, deleted_diagnostic_state, local_recovery_state, runtime_state
from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
from loop_manager_internal.storage.task_calls import CallDeferred, pending_for_plan


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

    def log_reference(workspace, owner):
        logs = Path(state_dir).resolve() / 'logs'
        if isinstance(workspace, str) and workspace and Path(workspace).is_absolute():
            logs = logs / hashlib.sha256(workspace.encode()).hexdigest()[:16]
        references.append((logs, owner))

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
    if 'diagnostic_preservations' in tables:
        for row in connection.execute('SELECT repo,scope,owner,payload FROM diagnostic_preservations'):
            try:
                payload = json.loads(row['payload'])
                if not isinstance(payload, dict):
                    raise ValueError('unknown diagnostic payload')
                if 'artifact_path' not in payload:
                    continue
                artifact = stored_path(payload['artifact_path'])
                owner = (row['repo'], row['owner']) if row['scope'] in ('plan', 'project') else (row['repo'], None)
            except (TypeError, ValueError, RecursionError):
                artifact, owner = Path(state_dir).resolve() / 'diagnostic-artifacts', (row['repo'], None)
            references.append((artifact, owner))
    if 'diagnostic_ref_uses' in tables:
        for row in connection.execute('SELECT repo,scope,owner,input_identity FROM diagnostic_ref_uses'):
            root = Path(state_dir).resolve() / 'diagnostic-artifacts'
            if isinstance(row['input_identity'], str) and re.fullmatch(r'[0-9a-f]{64}', row['input_identity']):
                artifact = root / row['input_identity']
                owner = (row['repo'], row['owner']) if row['scope'] in ('plan', 'project') else (row['repo'], None)
            else:
                artifact, owner = root, (row['repo'], None)
            references.append((artifact, owner))
    if 'task_call_reservations' in tables:
        references.extend((stored_path(row['receipt_path']), (row['repo'], row['plan_id'])) for row in connection.execute(
            'SELECT repo,plan_id,receipt_path FROM task_call_reservations WHERE receipt_path IS NOT NULL'))
    for row in connection.execute('SELECT repo,plan_id,worktree FROM exec_plans WHERE worktree IS NOT NULL'):
        log_reference(row['worktree'], (row['repo'], row['plan_id']))
    from loop_manager_internal.maintenance.maintenance_validation import _plan_id_from_path
    for project in connection.execute('SELECT * FROM projects'):
        if project['worktree'] is not None:
            try:
                plan_id = (None if runtime_state.planning_owns_execution_slot(project)
                           else _plan_id_from_path(project['plan_path'] or ''))
            except (TypeError, RecursionError):
                plan_id = None
            log_reference(project['worktree'], (project['repo'], plan_id))
        if project['planning_preserved_state']:
            try:
                workspace = runtime_state._preserved_planning_values(project['planning_preserved_state'])['worktree']
            except (runtime_state.RuntimeStateError, RecursionError):
                log_reference(None, (project['repo'], None))
            else:
                if workspace is not None:
                    log_reference(workspace, (project['repo'], None))
    if 'manual_work_groups' in tables:
        for group in connection.execute('SELECT repo,worktree FROM manual_work_groups WHERE worktree IS NOT NULL'):
            log_reference(group['worktree'], (group['repo'], None))
    if 'execplan_cancellations' in tables:
        for canceled in connection.execute('SELECT repo,plan_id,recovery_evidence FROM execplan_cancellations'):
            owner = (canceled['repo'], canceled['plan_id'])
            workspace = None
            try:
                saved = json.loads(canceled['recovery_evidence'])['row']
                if not isinstance(saved, dict) or (saved['repo'], saved['plan_id']) != owner:
                    raise ValueError('unattributed cancellation workspace')
                workspace = saved['worktree']
                if workspace is None:
                    continue
                if not isinstance(workspace, str) or not workspace or not Path(workspace).is_absolute():
                    raise ValueError('unconfirmed cancellation workspace')
            except (KeyError, TypeError, ValueError, RecursionError):
                # Unknown historical workspace ownership protects only logs;
                # independent receipt/collection cleanup can still proceed.
                workspace = None
            log_reference(workspace, owner)
    return [(candidate, owner) for path, owner in references for candidate in (path, path.resolve())]


def _independent_artifact(path, owner, references):
    candidate = path.resolve()
    return any(other != owner and (stored.is_relative_to(candidate) or candidate.is_relative_to(stored))
               for stored, other in references)


def _managed_artifact_path(path, state_dir):
    root, candidate = Path(state_dir).resolve(), path.resolve()
    return any(candidate.is_relative_to(root / folder) and candidate != root / folder
               for folder in ('agent-runtime', 'logs', 'diagnostic-artifacts'))


def remove_pending_files(connection, repo, state_dir):
    pending = set()
    for row in connection.execute('SELECT plan_id,path FROM deleted_plan_files WHERE repo=?', (repo,)).fetchall():
        path = Path(row['path'])
        # ponytail: O(queued paths * owners) keeps each destructive step fresh.
        # Index exact path/owner relations if queues grow; retain this recheck.
        try:
            deleted_plan_state.assert_no_historical_merge(connection, repo, row['plan_id'])
            state_roots = (Path(state_dir).absolute(), Path(state_dir).resolve())
            redirected_parent = any(parent.is_symlink() and any(parent != root and parent.is_relative_to(root)
                for root in state_roots) for parent in path.absolute().parents)
            shared = (not _managed_artifact_path(path, state_dir) or path.is_symlink() or redirected_parent
                or _independent_artifact(path, (repo, row['plan_id']), _artifact_references(connection, state_dir)))
        except (OSError, RuntimeError, TypeError, ValueError):
            shared = True  # Unconfirmed references retain this queue item.
        if shared:
            if row['plan_id'] not in pending:
                from loop_manager_internal.runtime.operation_events import emit
                emit('삭제 산출물 정리 대기: 독립 또는 미확인 참조를 보존합니다.',
                     project=repo, plan=row['plan_id'], stage='DELETED_PLAN_CLEANUP', resource=str(path))
            pending.add(row['plan_id'])
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as error:
            from loop_manager_internal.runtime.operation_events import emit
            emit('삭제 산출물 정리 대기: 파일 제거를 재시도합니다.', project=repo,
                 plan=row['plan_id'], stage='DELETED_PLAN_CLEANUP', resource=str(path), error=str(error))
            pending.add(row['plan_id'])
            continue
        with runtime_state._transaction(connection):
            connection.execute('DELETE FROM deleted_plan_files WHERE repo=? AND plan_id=? AND path=?',
                               (repo, row['plan_id'], row['path']))
    return pending


def artifact_paths(connection, repo, row, state_dir, *, extra_worktrees=()):
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
        if not shared and _managed_artifact_path(receipt.parent, state_dir):
            paths.append(receipt.parent)
        elif receipt.name.endswith('.process.json'):
            paths.extend((receipt, receipt.with_name(receipt.name.removesuffix('.process.json'))))
        else:
            raise CallDeferred('공유 폴더의 호출 산출물 귀속을 확인할 수 없습니다.')
    for workspace in (*((row['worktree'],) if row['worktree'] else ()), *extra_worktrees):
        paths.append(Path(state_dir) / 'logs' / hashlib.sha256(workspace.encode()).hexdigest()[:16])
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='targeted_test_collections'").fetchone():
        for collection in connection.execute('SELECT c.directory FROM targeted_test_collections c '
                'JOIN agent_invocations a ON a.invocation_id=c.invocation_id WHERE a.repo=? AND a.plan_id=?', owner):
            path = Path(collection['directory']).absolute()
            if not any(path.resolve().is_relative_to(parent.resolve()) for parent in paths):
                paths.append(path)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_preservations'").fetchone():
        from loop_manager_internal.recovery.diagnostic_preservation import load_verified_artifacts
        for saved in connection.execute('SELECT * FROM diagnostic_preservations WHERE repo=? AND owner=?', owner):
            try:
                payload = json.loads(saved['payload'])
                if not isinstance(payload, dict):
                    raise ValueError('unknown diagnostic payload')
                if 'artifact_path' not in payload:
                    continue
                if saved['scope'] not in ('plan', 'project'):
                    raise ValueError('unknown diagnostic owner scope')
                load_verified_artifacts(state_dir, saved)
                paths.append(Path(payload['artifact_path']))
            except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
                raise CallDeferred('진단 원문 bundle의 실제 귀속·내용을 확인할 수 없습니다.') from error
    if any(path.is_symlink() or _independent_artifact(path, owner, references) for path in paths):
        raise CallDeferred('독립 작업이 참조하는 공유 산출물은 제거할 수 없습니다.')
    if any(not _managed_artifact_path(path, state_dir) for path in paths):
        raise CallDeferred('삭제 산출물의 관리 경로를 확인할 수 없어 작업 기록을 보존합니다.')
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


def retired_baseline_refs(row, repo):
    """Read only the native baseline identity; physical ref removal stays elsewhere."""
    if row.get('agent_baseline') is None:
        return ()
    from loop_manager_internal.agents.scope_baseline import _deserialize_agent_baseline
    from loop_manager_internal.agents.scope_contracts import GitSnapshot
    try:
        payload = json.loads(row['agent_baseline'])
        baseline = _deserialize_agent_baseline(row['agent_baseline'])
        scope = GitSnapshot.deserialize(row['agent_snapshot'])
        if (not isinstance(payload, dict) or set(payload) - {
                'version', 'ref', 'ref_target', 'index_tree', 'worktree_tree', 'stage_trees'}
                or type(payload['version']) is not int or payload['version'] != 1
                or not row['branch'] or scope.branch not in (row['branch'], 'refs/heads/' + row['branch'])
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', scope.head)
                or any(not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', baseline[field])
                       for field in ('ref_target', 'index_tree', 'worktree_tree'))):
            raise ValueError('unknown baseline binding')
        stages = payload.get('stage_trees', {})
        if (not isinstance(stages, dict) or set(stages) - {'0', '1', '2', '3'}
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value)
                       for value in stages.values())):
            raise ValueError('unknown baseline index stages')
        identity = scope.head + '\0' + baseline['index_tree'] + '\0' + baseline['worktree_tree']
        if stages:
            identity += '\0' + json.dumps(stages, sort_keys=True)
        expected = 'refs/loop-manager/baselines/' + hashlib.sha256(repo.encode()).hexdigest()[:16] + '/' + hashlib.sha256(identity.encode('ascii')).hexdigest()
        if baseline['ref'] != expected:
            raise ValueError('baseline repository or content differs')
        return (expected,)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise CallDeferred('baseline의 저장 자원 귀속을 확인할 수 없습니다.') from error


def retired_integration_baselines(snapshot, row, repo):
    """Inventory native preservation refs in current and historical integrations."""
    refs = []
    for field in ('base_rebase_restart', 'work_branch_integration'):
        frames = [*snapshot.get(field + '_history', []), *([snapshot[field]] if snapshot.get(field) else [])]
        for frame in frames:
            for item in (frame, *([frame['restart']] if 'restart' in frame else [])):
                if item.get('baseline') is not None:
                    source = item.get('snapshot', item.get('before'))
                    refs.extend(retired_baseline_refs({**row, 'agent_baseline': item['baseline'],
                                                       'agent_snapshot': source}, repo))
    for previous in (*snapshot.get('rebase_operation_history', []),
                     *(item['snapshot'] for item in snapshot.get('superseded_rebases', []))):
        refs.extend(retired_integration_baselines(previous, row, repo))
    return tuple(dict.fromkeys(refs))


def retired_restart_resources(snapshot, row, repo, state_dir):
    """Inventory native retired copies; live removal remains with the resource owner."""
    from loop_manager_internal.storage.execution_checkpoint_state import cancelled_branch_restart
    resources, baselines = [], []
    intent = cancelled_branch_restart(snapshot, repo, row['plan_id'], row)
    if intent is not None:
        old = intent['plan']
        source = Path(old['worktree'])
        if (source.name != row['plan_id'] or source.resolve().parent.parent != Path(state_dir).resolve() / 'worktrees'
                or source.is_symlink()):
            raise CallDeferred('퇴역 재시작 원본의 관리 경로를 확인할 수 없습니다.')
        resources.append((intent['archive_branch'], intent['archive']))
        baselines.extend(retired_baseline_refs(old, repo))
        baselines.extend(retired_integration_baselines(intent['checkpoint'], old, repo))
        copies, refs = retired_restart_resources(intent['checkpoint'], old, repo, state_dir)
        resources.extend(copies)
        baselines.extend(refs)
    for previous in (*snapshot.get('rebase_operation_history', []),
                     *(item['snapshot'] for item in snapshot.get('superseded_rebases', []))):
        copies, refs = retired_restart_resources(previous, row, repo, state_dir)
        resources.extend(copies)
        baselines.extend(refs)
    return tuple(dict.fromkeys(resources)), tuple(dict.fromkeys(baselines))


def _rowless_cancellations(connection, repo, state_dir):
    """Read complete audits whose saved resources can be proved absent now."""
    from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_cancellations'").fetchone():
        return
    for canceled in connection.execute('SELECT * FROM execplan_cancellations WHERE repo=?', (repo,)).fetchall():
        if connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?', (repo, canceled['plan_id'])).fetchone():
            continue
        try:
            audit = json.loads(canceled['recovery_evidence'] or '{}')
            saved = audit['row']
            started = isinstance(saved, dict) and saved.get('ever_started') == 1
            historical_fields = {
                'active_sha', 'validated_base_sha', 'last_error', 'blocked_fingerprint',
                'blocked_worktree_fingerprint', 'blocked_review_fingerprint', 'worker_session_id',
                'reviewer_session_id', 'worker_result', 'verify_result', 'review_result',
                'blocked_reason', 'guidance_plan_hash',
            } if started else set()
            retired_workspace = (saved.get('workspace_owned') == 1
                and saved.get('branch') == 'loop-manager/' + canceled['plan_id']
                and isinstance(saved.get('worktree'), str) and Path(saved['worktree']).is_absolute()
                and Path(saved['worktree']).name == canceled['plan_id']
                and Path(saved['worktree']).resolve().parent.parent == Path(state_dir).resolve() / 'worktrees'
                and not Path(saved['worktree']).is_symlink() and not Path(saved['worktree']).exists())
            if retired_workspace:
                historical_fields |= {'branch', 'worktree', 'publication_remote_oid', 'pr_url', 'pr_head_oid'}
                if (saved.get('pr_url') is not None and (not isinstance(saved['pr_url'], str) or not saved['pr_url'].strip())
                        or saved.get('pr_head_oid') is not None and (not isinstance(saved['pr_head_oid'], str)
                            or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', saved['pr_head_oid']))):
                    continue
                from loop_manager_internal.storage.execution_checkpoint_state import historical_agent_snapshots_match
                if not historical_agent_snapshots_match(saved, (saved[field] for field in (
                        'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot'))):
                    continue
                if saved['agent_input_fingerprint'] is not None and (not isinstance(saved['agent_input_fingerprint'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', saved['agent_input_fingerprint'])):
                    continue
                if any(saved[field] is not None and (not isinstance(saved[field], str)
                           or catalog.parse_frontmatter(saved[field]).get('id') != saved['plan_id'])
                       for field in ('agent_input_plan', 'agent_result_plan')):
                    continue
                historical_fields |= {'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot',
                                      'agent_input_fingerprint', 'agent_input_plan', 'agent_result_plan'}
                retired_baseline_refs(saved, repo)
                historical_fields.add('agent_baseline')
            allowed_statuses = {'READY'}
            if started:
                allowed_statuses.add('PLAN_BLOCKED')
            if retired_workspace:
                allowed_statuses.update(('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'))
            integration_refs, restart_resources = (), ()
            checkpoints = audit.get('checkpoints')
            if checkpoints is not None and not isinstance(checkpoints, dict):
                continue
            if checkpoints:
                if set(checkpoints) - {'execution_checkpoints', 'blocked_checkpoints'}:
                    continue
                if 'blocked_checkpoints' in checkpoints:
                    from loop_manager_internal.storage.blocked_checkpoint_state import (
                        cancelled_checkpoint_without_resources, cancelled_checkpoint_after_resource_retirement)
                    if retired_workspace:
                        valid = cancelled_checkpoint_after_resource_retirement(checkpoints['blocked_checkpoints'], repo,
                            canceled['plan_id'], saved, block=audit.get('block'))
                    else:
                        valid = cancelled_checkpoint_without_resources(checkpoints['blocked_checkpoints'], repo,
                            canceled['plan_id'], block=audit.get('block'))
                    if not valid:
                        continue
            if checkpoints and 'execution_checkpoints' in checkpoints:
                checkpoint = checkpoints['execution_checkpoints']
                if (not isinstance(checkpoint, dict) or set(checkpoint) != {
                        'repo', 'plan_id', 'generation', 'operation', 'expected_head', 'target_base_oid',
                        'observed_remote_oid', 'snapshot', 'workspace_ownership_held'}
                        or checkpoint['repo'] != canceled['repo'] or checkpoint['plan_id'] != canceled['plan_id']
                        or type(checkpoint['generation']) is not int or checkpoint['generation'] < 0
                        or type(checkpoint['workspace_ownership_held']) is not int or checkpoint['workspace_ownership_held'] != 0
                        or checkpoint['operation'] is not None
                        or any(checkpoint[field] is not None and (not retired_workspace
                            or not isinstance(checkpoint[field], str)
                            or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', checkpoint[field]))
                            for field in ('expected_head', 'target_base_oid', 'observed_remote_oid'))):
                    continue
                snapshot = json.loads(checkpoint['snapshot']) if checkpoint['snapshot'] is not None else {}
                from loop_manager_internal.storage.execution_checkpoint_state import cancelled_snapshot_without_new_resources
                if not cancelled_snapshot_without_new_resources(snapshot, repo, canceled['plan_id'], saved):
                    continue
                integration_refs = retired_integration_baselines(snapshot, saved, repo)
                restart_resources, restart_refs = retired_restart_resources(snapshot, saved, repo, state_dir)
                integration_refs = (*integration_refs, *restart_refs)
                deleted_document_cleanup.document_resources(snapshot, state_dir, repo, canceled['plan_id'])
            judgment = audit.get('judgment')
            decision = judgment.get('decision') if isinstance(judgment, dict) else None
            decision = decision if isinstance(decision, dict) else {}
            confirmed = (decision.get('id') == saved['plan_id'] and decision.get('outcome') == 'withdrawn'
                and decision.get('source') in json.loads(saved['sources'])
                and isinstance(decision.get('reason'), str) and bool(decision['reason'].strip()))
            affected = audit.get('affected', [])
            if not isinstance(affected, list) or any(not isinstance(other, str) for other in affected):
                continue
            from loop_manager_internal.storage.execplan_catalog_state import is_cancelled
            dependents = tuple(other for other in affected if not is_cancelled(connection, repo, other) and connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=? AND status<>'MERGED'", (repo, other)).fetchone())
            if started and (type(audit.get('cleanup_pending')) is not bool
                    or audit['cleanup_pending'] and not retired_workspace):
                continue
            if started and not connection.execute(
                    "SELECT 1 FROM agent_invocations WHERE repo=? AND plan_id=? "
                    "AND execution_status='executed' AND execution_started_at IS NOT NULL",
                    (repo, canceled['plan_id'])).fetchone():
                continue  # Missing historical calls cannot prove a started writer's termination.
            if (not isinstance(saved, dict) or saved['status'] not in allowed_statuses
                    or type(saved['ever_started']) is not int or saved['ever_started'] not in (0, 1)
                    or type(saved['workspace_owned']) is not int or saved['workspace_owned'] != (1 if retired_workspace else 0)
                    or type(saved['retry_count']) is not int or saved['retry_count'] < 0
                    or (not started and saved['retry_count'] != 0) or saved['pr_conflict_recovery'] != 0
                    or any(not isinstance(json.loads(saved[field]), dict) or not started and saved[field] != '{}'
                           for field in ('verify_failure_history', 'review_failure_history', 'review_level_history'))
                    or any(saved[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS if field not in historical_fields)
                    or any(saved[field] is not None for field in ('pr_conflict_head_oid', 'pr_conflict_base_oid'))
                    or audit.get('cleanup_pending') and not retired_workspace
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
        yield {**saved, '_retired_baseline_refs': integration_refs, '_retired_restart_resources': restart_resources,
               '_withdrawal_confirmed': confirmed, '_withdrawal_dependents': dependents}, dict(canceled)


def _rowless_holds(connection, repo, pinned):
    """Reuse published reference recovery to retire never-registered holds."""
    from loop_manager_internal.storage.execplan_catalog_state import verified_historical_merge_ids
    historical = verified_historical_merge_ids(connection, repo)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for held in connection.execute('SELECT * FROM execplan_catalog_holds WHERE repo=?', (repo,)).fetchall():
        key = held['plan_id']
        if key in historical or held['resume_status'] is not None or any(
                connection.execute('SELECT 1 FROM ' + table + ' WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
                for table in ('exec_plans', 'execplan_cancellations', 'agent_invocations',
                              'execution_checkpoints', 'blocked_checkpoints') if table in tables):
            continue
        try:
            evidence = json.loads(held['evidence'])
            goal = evidence['deleted_goal']
            original = goal['row']
            path, text = goal['path'], goal['text']
            plan = catalog.parse_execplan_text(path, text, validate_sources=False)
            revision, deletion = goal['original_revision'], goal['observed_revision']
            if (plan['id'] != key or original['plan_id'] != key or original['path'] != path
                    or evidence['path'] != path or original['plan_sha'] != revision
                    or original['current_revision_sha'] != revision
                    or original['content_hash'] != catalog.plan_content_hash(text)
                    or evidence['content_hash'] != original['content_hash']
                    or not all(re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', item) for item in (revision, deletion))
                    or plan['title'] != original['title'] or plan['sources'] != json.loads(original['sources'])
                    or plan['depends_on'] != json.loads(original['depends_on'])):
                continue
            approvals = evidence['resolved_references']
            if not isinstance(approvals, dict) or not approvals:
                continue  # Absence alone never proves the held goal was withdrawn.
            dependents, approval_revisions = [], []
            for other, approval in approvals.items():
                current = connection.execute('SELECT path,content_hash FROM exec_plans WHERE repo=? AND plan_id=?',
                                             (repo, other)).fetchone()
                entry = pinned.snapshot.all.get(other)
                if (not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', approval['revision'])
                        or current is None or entry is None or current['path'] != approval['path']
                        or current['content_hash'] != approval['content_hash'] or entry.path != current['path']
                        or catalog.plan_content_hash(entry.text) != current['content_hash']):
                    raise ValueError('published recovery declaration changed')
                # The existing recovery validator/cleanup helper verifies the actual
                # Decision Log, source, replacement dependencies and current graph.
                dependents.append(other)
                approval_revisions.append(approval['revision'])
            if not isinstance(goal['affected'], list) or set(goal['affected']) - set(approvals):
                continue
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue
        yield dict(plan_id=key, path=path, sources=original['sources'], branch=None, worktree=None,
                   pr_url=None, publication_remote_oid=None, plan_sha=revision, current_revision_sha=revision,
                   content_hash=original['content_hash'], _deletion_revision=deletion,
                   _withdrawal_dependents=tuple(dependents), _approval_revisions=tuple(approval_revisions)), dict(held)


def _marked_diagnostic_owners(connection, repo):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for key in deleted_diagnostic_state.marked_owners(connection, repo):
        if any(connection.execute('SELECT 1 FROM ' + table + ' WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
               for table in ('exec_plans', 'execplan_cancellations', 'execplan_catalog_holds',
                             'execution_checkpoints', 'blocked_checkpoints') if table in tables):
            continue
        marker = dict(connection.execute('SELECT * FROM deleted_plan_ids WHERE repo=? AND plan_id=?', (repo, key)).fetchone())
        yield dict(plan_id=key, path='', sources='[]', branch=None, worktree=None, pr_url=None,
                   publication_remote_oid=None, _final_erased=True), marker


@contextmanager
def _rowless_ownership(connection, state_dir, repo, row, canceled):
    from loop_manager_internal.runtime.resource_lock import resource_locks
    from loop_manager_internal.storage import task_calls, work_claims
    from loop_manager_internal.maintenance.maintenance_validation import _plan_id_from_path
    key = row['plan_id']
    resources = ['plan:' + key]
    resources += [prefix + row[field] for prefix, field in (('branch:', 'branch'), ('worktree:', 'worktree')) if row[field]]
    if row['worktree']:
        resources.append('git:' + str(Path(row['worktree']).resolve()))
    with resource_locks(Path(state_dir), repo, resources, owner='deleted-plan-cleanup'):
        if connection.in_transaction:
            raise CallDeferred('row 없는 삭제 대상의 외부 관측은 DB transaction 밖에서 수행해야 합니다.')
        def check():
            table = ('execplan_cancellations' if 'recovery_evidence' in canceled else
                     'execplan_catalog_holds' if 'evidence' in canceled else 'deleted_plan_ids')
            current = connection.execute('SELECT * FROM ' + table + ' WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
            project = connection.execute('SELECT plan_path FROM projects WHERE repo=?', (repo,)).fetchone()
            if table == 'deleted_plan_ids':
                tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(connection.execute('SELECT 1 FROM ' + store + ' WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
                       for store in ('execplan_cancellations', 'execplan_catalog_holds', 'execution_checkpoints', 'blocked_checkpoints') if store in tables):
                    raise CallDeferred('최종 삭제 ID에 새 미확정 수명이 기록됐습니다.')
            if table == 'execplan_catalog_holds':
                tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(connection.execute('SELECT 1 FROM ' + store + ' WHERE repo=? AND plan_id=?',
                                          (repo, key)).fetchone() for store in (
                        'execplan_cancellations', 'agent_invocations', 'execution_checkpoints', 'blocked_checkpoints') if store in tables):
                    raise CallDeferred('등록 전 hold의 실행·취소 근거가 변경됐습니다.')
                for other, approval in json.loads(canceled['evidence'])['resolved_references'].items():
                    dependent = connection.execute('SELECT path,content_hash FROM exec_plans WHERE repo=? AND plan_id=?',
                                                   (repo, other)).fetchone()
                    if dependent is None or any(dependent[field] != approval[field] for field in ('path', 'content_hash')):
                        raise CallDeferred('등록 전 hold의 게시된 참조 복구 선언이 변경됐습니다.')
            if (current is None or dict(current) != canceled
                    or project is not None and _plan_id_from_path(project['plan_path'] or '') == key
                    or connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
                    or task_calls.pending_for_plan(connection, repo, key)):
                raise CallDeferred('row 없는 삭제 대상의 감사·writer 상태가 변경됐습니다.')
            if row['worktree'] and (Path(row['worktree']).is_symlink() or Path(row['worktree']).exists()
                    or connection.execute('SELECT 1 FROM exec_plans WHERE worktree=? OR (repo=? AND branch=?)',
                                          (row['worktree'], repo, row['branch'])).fetchone()
                    or connection.execute('SELECT 1 FROM projects WHERE worktree=? OR (repo=? AND branch=?)',
                                          (row['worktree'], repo, row['branch'])).fetchone()):
                raise CallDeferred('이전 취소 자원이 다시 사용 중이어서 감사를 보존합니다.')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone() and connection.execute(
                    'SELECT 1 FROM execution_checkpoints WHERE repo=? AND plan_id=? AND operation IS NOT NULL', (repo, key)).fetchone():
                raise CallDeferred('row 없는 삭제 대상의 미확정 Git operation이 남아 있습니다.')
            try:
                _state, owned = work_claims.manual_plan_ownership(connection, repo, key,
                    row if table == 'execplan_cancellations' else None)
                ids, paths = work_claims.publication_claims(connection, repo)
                if owned or key in ids or {row['path'], *json.loads(row['sources'])} & paths:
                    raise CallDeferred('row 없는 삭제 대상의 수동·게시 소유권이 남아 있습니다.')
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise CallDeferred('row 없는 삭제 대상의 현재 소유권을 확인할 수 없습니다.') from error
        check()
        yield SimpleNamespace(check=check)


def cleanup(connection, repo_path, repo, pinned, references, *, git, git_bytes, pr_for_branch, state_dir, revision_is_current,
            assess_cleanup=None, remove_cleanup_worktree=None, recover_cleanup_worktree=None):
    """Observe outside the DB writer; recheck under task resource leases before erasure."""
    remove_pending_files(connection, repo, state_dir)
    if pinned.regular_paths is None or pr_for_branch is None:
        return ()
    deleted = []
    rows = [(row, None) for row in connection.execute("SELECT * FROM exec_plans WHERE repo=? AND status!='MERGED' AND agent_role IS NULL", (repo,)).fetchall()]
    rows.extend(_rowless_cancellations(connection, repo, state_dir))
    rows.extend(_rowless_holds(connection, repo, pinned))
    rows.extend(_marked_diagnostic_owners(connection, repo))
    for row, canceled in rows:
        row = dict(row)
        key, path = row['plan_id'], row['path']
        if not isinstance(key, str) or not re.fullmatch(r'G-\d{3,}', key):
            continue
        if path in pinned.regular_paths or key in pinned.snapshot.observed_ids:
            continue
        if row.get('_final_erased'):
            resolved = {}  # The existing final marker is authority; no historical declaration is synthesized.
        else:
            revision = row['current_revision_sha'] or row['plan_sha']
            if not revision:
                continue
            if git(repo_path, 'merge-base', '--is-ancestor', revision, pinned.revision_sha, check=False).returncode:
                continue
            if canceled is not None and git(repo_path, 'merge-base', '--is-ancestor', canceled.get('deletion_commit', row.get('_deletion_revision')), pinned.revision_sha, check=False).returncode:
                continue
            if any(git(repo_path, 'merge-base', '--is-ancestor', approved, pinned.revision_sha, check=False).returncode
                   for approved in row.get('_approval_revisions', ())):
                continue
            contents = git_bytes(repo_path, 'show', f'{revision}:{path}')
            if catalog.plan_content_hash(contents) != row['content_hash']:
                continue
            documents = dict(pinned.snapshot.all)
            # Withheld declarations remain available for goal recovery, never execution.
            for held_key, held_path, _hash in pinned.snapshot.withheld:
                text = git_bytes(repo_path, 'show', f'{pinned.revision_sha}:{held_path}').decode('utf-8')
                documents[held_key] = SimpleNamespace(path=held_path, text=text)
            related = (*references, *(SimpleNamespace(plan_id=other, source=path)
                        for other in row.get('_withdrawal_dependents', ())))
            resolved = resolved_references(key, related, documents)
            if resolved is None:
                continue
            if not resolved and not row.get('_withdrawal_confirmed'):
                continue  # Plain file deletion is ordinary cancellation, not authority to erase its audit.
        from .remote_deletions import historical_reference_ids
        historical_ids = historical_reference_ids(connection, repo, pinned)
        historical_dependencies = tuple(dict(other) for other in connection.execute(
            'SELECT * FROM exec_plans WHERE repo=? AND plan_id<>?', (repo, key))
            if other['plan_id'] in historical_ids and key in json.loads(other['depends_on']))
        if any(key in json.loads(other['depends_on']) and other['plan_id'] not in resolved and other['plan_id'] not in historical_ids
               for other in connection.execute('SELECT plan_id,depends_on FROM exec_plans WHERE repo=? AND plan_id<>?', (repo, key))):
            continue
        if row['branch'] and row['branch'] != 'loop-manager/' + key:
            continue
        related_rows = {other: connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                                 (repo, other)).fetchone() for other in resolved}
        if any(record is None for record in related_rows.values()):
            continue
        related_rows = {other: dict(record) for other, record in related_rows.items()}
        try:
            deleted_plan_state.assert_no_historical_merge(connection, repo, key)
            from loop_manager_internal.storage.common_defect_state import assert_plan_erasure_available
            assert_plan_erasure_available(connection, repo, key)
            diagnostics = deleted_diagnostic_state.capture(connection, repo, key)
            extra_baselines = row.get('_retired_baseline_refs', ())
            restart_resources = row.get('_retired_restart_resources', ())
            if canceled is None and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone():
                checkpoint = connection.execute('SELECT snapshot FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
                if checkpoint is not None:
                    try:
                        saved = json.loads(checkpoint['snapshot'] or '{}')
                        extra_baselines = retired_integration_baselines(saved, row, repo)
                        restart_resources, restart_refs = retired_restart_resources(saved, row, repo, state_dir)
                        extra_baselines = (*extra_baselines, *restart_refs)
                    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, RecursionError) as error:
                        raise CallDeferred('과거 통합의 baseline 소유권을 확인할 수 없습니다.') from error
            baselines = tuple(dict.fromkeys((*retired_baseline_refs(row, repo), *extra_baselines)))
            diagnostic_resources = ['diagnostic:' + ref for ref in diagnostics['refs']] + ['git:' + ref for ref in baselines]
            diagnostic_resources += [prefix + path for path in diagnostics['worktrees'] for prefix in ('worktree:', 'git:')]
            diagnostic_resources += [prefix + branch for branch, path in restart_resources for prefix in ('branch:', 'git:')]
            diagnostic_resources += [prefix + path for branch, path in restart_resources for prefix in ('worktree:', 'git:')]
            diagnostic_resources = [resource for resource in diagnostic_resources if not resource_owned(Path(state_dir), repo, resource)]
            lease = (reserved_workspace_ownership(connection, state_dir, repo, key) if canceled is None
                     else _rowless_ownership(connection, state_dir, repo, row, canceled))
            with lease as ownership, resource_locks(Path(state_dir), repo, diagnostic_resources, owner='deleted-plan-diagnostics'), ExitStack() as document_leases:
                document_cleanup = None
                def check_restarts(*, external=False):
                    for branch, raw in restart_resources:
                        workspace = Path(raw)
                        if (workspace.exists() or workspace.is_symlink()
                                or any(parent.is_symlink() for parent in workspace.parents)):
                            raise CallDeferred('퇴역 재시작 사본의 실제 정리가 남아 있습니다.')
                        deleted_document_cleanup.check_workspace_references(connection, raw)
                        for table in ('exec_plans', 'projects', 'manual_work_groups'):
                            columns = {item[1] for item in connection.execute('PRAGMA table_info(' + table + ')')}
                            if {'repo', 'worktree', 'branch'} <= columns and connection.execute(
                                    'SELECT 1 FROM ' + table + ' WHERE worktree=? OR (repo=? AND branch=?)',
                                    (raw, repo, branch)).fetchone():
                                raise CallDeferred('독립 작업이 참조하는 퇴역 재시작 자원을 보존합니다.')
                        if connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_groups'").fetchone() and connection.execute(
                                'SELECT 1 FROM manual_work_groups WHERE repo=? AND source_branch=?', (repo, branch)).fetchone():
                            raise CallDeferred('수동 source가 참조하는 퇴역 ref를 보존합니다.')
                        if external:
                            from loop_manager_internal.recovery.checkpoint_adapter import _frozen_push_target
                            origin = (diagnostics['project'] or {}).get('origin')
                            options, operand = _frozen_push_target(origin) if origin else ((), 'origin')
                            try:
                                remote = git(repo_path, *options, 'ls-remote', '--heads', operand, 'refs/heads/' + branch, check=False)
                                local = git(repo_path, 'rev-parse', '--verify', 'refs/heads/' + branch, check=False)
                                pr = pr_for_branch(repo_path, branch)
                                registered = git(repo_path, 'worktree', 'list', '--porcelain')
                            except (OSError, RuntimeError, ValueError) as error:
                                raise CallDeferred('퇴역 재시작 자원의 현재 관측을 확인할 수 없습니다.') from error
                            if (remote.returncode or remote.stdout.strip() or local.returncode not in (1, 128)
                                    or pr is not None or registered.returncode or any(line in {
                                        'worktree ' + raw, 'branch refs/heads/' + branch} for line in registered.stdout.splitlines())):
                                raise CallDeferred('퇴역 재시작 ref·PR·등록의 정리가 남아 있습니다.')
                def check_baselines():
                    for ref in baselines:
                        try:
                            result = git(repo_path, 'rev-parse', '--verify', ref, check=False)
                        except (OSError, RuntimeError) as error:
                            raise CallDeferred('baseline ref의 현재 부재를 관측할 수 없습니다.') from error
                        if result.returncode not in (1, 128):
                            raise CallDeferred('baseline ref의 실제 정리가 아직 확인되지 않았습니다.')
                def check_ownership():
                    ownership.check()
                    check_restarts()
                    if deleted_diagnostic_state.capture(connection, repo, key) != diagnostics:
                        raise CallDeferred('진단 소유 기록이 관측 중 변경됐습니다.')
                    if any(Path(path).exists() or Path(path).is_symlink() for path in diagnostics['worktrees']):
                        raise CallDeferred('진단 원본 worktree의 정리가 아직 끝나지 않았습니다.')
                    if pending_for_plan(connection, repo, key):
                        raise CallDeferred('삭제 전에 실제 호출 종료·결과 인계·예약 반환을 확인해야 합니다.')
                    if document_cleanup is not None:
                        document_cleanup.check()
                    for expected in historical_dependencies:
                        current = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, expected['plan_id'])).fetchone()
                        if current is None or dict(current) != expected:
                            raise CallDeferred('과거 Merge 참조가 관측 중 변경됐습니다.')
                    for other, expected in related_rows.items():
                        current = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, other)).fetchone()
                        if current is None or dict(current) != expected:
                            raise CallDeferred('삭제 대상의 참조 복구 row가 관측 중 변경됐습니다.')
                if not revision_is_current():
                    continue
                check_ownership()
                for ref in diagnostics['refs']:
                    try:
                        from loop_manager_internal.recovery.checkpoint_adapter import _frozen_push_target
                        origin = (diagnostics['project'] or {}).get('origin')
                        options, operand = _frozen_push_target(origin) if origin else ((), 'origin')
                        remote = git(repo_path, *options, 'ls-remote', '--heads', operand, 'refs/heads/' + ref, check=False)
                        local = git(repo_path, 'rev-parse', '--verify', 'refs/heads/' + ref, check=False)
                        pull_request = pr_for_branch(repo_path, ref)
                    except (OSError, RuntimeError, ValueError) as error:
                        raise CallDeferred('진단 ref·PR의 원격 관측을 확인할 수 없습니다.') from error
                    if (remote.returncode or remote.stdout.strip() or local.returncode not in (1, 128)
                            or pull_request is not None):
                        raise CallDeferred('진단 ref·PR의 현재 부재를 확인할 수 없습니다.')
                check_baselines()
                check_restarts(external=True)
                if diagnostics['worktrees']:
                    registered = git(repo_path, 'worktree', 'list', '--porcelain')
                    if registered.returncode or any('worktree ' + path in registered.stdout.splitlines() for path in diagnostics['worktrees']):
                        raise CallDeferred('진단 원본의 worktree 등록 정리가 남아 있습니다.')
                if canceled is not None:
                    canonical = 'loop-manager/' + key
                    try:
                        remote = git(repo_path, 'ls-remote', '--heads', 'origin', 'refs/heads/' + canonical, check=False)
                        local = git(repo_path, 'rev-parse', '--verify', 'refs/heads/' + canonical, check=False)
                        pr = pr_for_branch(repo_path, canonical)
                    except (OSError, RuntimeError) as error:
                        raise CallDeferred('row 없는 삭제 대상의 실제 PR·ref를 확인할 수 없습니다.') from error
                    registered = git(repo_path, 'worktree', 'list', '--porcelain') if row['worktree'] else None
                    closed_history = (pr is not None and pr.get('state') == 'CLOSED' and not pr.get('mergedAt')
                        and pr.get('headRefName') == canonical and row.get('pr_url')
                        and pr.get('url') == row['pr_url'])
                    if (pr is not None and not closed_history or remote.returncode or remote.stdout.strip() or local.returncode not in (1, 128)
                            or registered is not None and (registered.returncode or any(
                                line in {'worktree ' + row['worktree'], 'branch refs/heads/' + canonical}
                                for line in registered.stdout.splitlines()))):
                        continue
                pr = pr_for_branch(repo_path, row['branch'], row['pr_url']) if row['branch'] else None
                if row['pr_url'] and (pr is None or pr.get('url') != row['pr_url']):
                    continue
                if pr is not None and (pr.get('state') != 'CLOSED' or pr.get('mergedAt')
                        or pr.get('headRefName') != row['branch']):
                    continue
                check_ownership()
                if canceled is None and dict(connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()) != dict(row):
                    continue
                from loop_manager_internal.git.manager_git_journal import has_pending_project_mutation
                if has_pending_project_mutation(Path(state_dir), repo, targets={row['worktree'], row['branch']}):
                    continue
                if canceled is not None and has_pending_project_mutation(Path(state_dir), repo,
                        recoverable=lambda intent: (intent['owner'] != 'plan:' + key or recover_cleanup_worktree is not None
                            and deleted_document_cleanup.removal_recoverable(connection, state_dir, repo, key, canceled, intent))):
                    continue
                # shortcut: surviving remote refs stay pending until lease-aware deletion is available.
                if row['branch'] and git(repo_path, 'ls-remote', '--heads', 'origin', f"refs/heads/{row['branch']}").stdout.strip():
                    continue
                document_cleanup = document_leases.enter_context(deleted_document_cleanup.cleaned_documents(
                    connection, state_dir, repo_path, repo, key, canceled, git=git,
                    assess_cleanup=assess_cleanup, remove_cleanup_worktree=remove_cleanup_worktree,
                    recover_cleanup_worktree=recover_cleanup_worktree, check_owner=check_ownership))
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
                        check_ownership()
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
                        check_ownership()
                        git(repo_path, 'update-ref', '-d', ref, local.stdout.strip())
                if not revision_is_current():
                    continue
                check_ownership()
                check_baselines()
                check_restarts(external=True)
                artifacts = artifact_paths(connection, repo, row, state_dir, extra_worktrees=(*document_cleanup.worktrees, *diagnostics['worktrees'], *(path for branch, path in restart_resources)))
                with runtime_state._transaction(connection):
                    check_ownership()
                    for other, dependencies in resolved.items():
                        connection.execute('UPDATE exec_plans SET depends_on=? WHERE repo=? AND plan_id=?',
                                           (json.dumps(dependencies, ensure_ascii=False, separators=(',', ':')), repo, other))
                    deleted_plan_state.forget(connection, repo, key, artifacts=artifacts, diagnostic_snapshot=diagnostics,
                        historical_dependencies=historical_dependencies)
                    release_reference_holds(connection, repo, key, path, resolved)
                if key not in remove_pending_files(connection, repo, state_dir):
                    deleted.append(key)
        except (CallDeferred, local_recovery_state.LocalRecoveryRequestError):
            continue
    return tuple(deleted)
