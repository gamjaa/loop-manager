"""Pinned Requirement/ExecPlan selection and resumable, file-local application."""
from __future__ import annotations

import base64
import hashlib
import json
import re

from loop_manager_internal.catalog import catalog_service, requirement_sources
from loop_manager_internal.catalog.frontmatter import requirement_identity
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.git.command_adapter import revision_requirement_documents
from loop_manager_internal.git.rebase_correction import parse_unmerged
from loop_manager_internal.git.managed_rebase_validation import _safe_path
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason


def document_path(path):
    return bool(':' not in path and '\0' not in path and re.fullmatch(r'docs/(?:requirements|plans)/(?:archive/)?[^/\\]+\.md', path)
                and all(part not in {'.', '..'} for part in path.split('/')))


def has_pending_documents(checkpoint, observed):
    item = (checkpoint.snapshot or {}).get('rebase_documents')
    return bool(item and item.get('state') != 'staged' or
                any(entry.stage and document_path(entry.path) for entry in observed.index))


def select_paths(conflicts, catalog, requirements, original_catalog=None):
    """Validate IDs and include the pinned rename/archive destination, if any."""
    selected = set(path for path in conflicts if document_path(path))
    for path in tuple(selected):
        if path.startswith('docs/plans/'):
            entry = next((entry for entry in catalog.all.values() if entry.path == path), None)
            if entry is None and original_catalog is not None:
                old = next((entry for entry in original_catalog.all.values() if entry.path == path), None)
                if old is None:
                    raise RuntimeError('deleted ExecPlan identity unavailable: ' + path)
                entry = catalog.all.get(old.plan_id)
            if entry:
                selected.add(entry.path)
        elif path in requirements:
            raw = requirements[path]
            requirement_identity(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
        else:
            source = path.replace('docs/requirements/archive/', 'docs/requirements/', 1)
            resolution = requirement_sources.resolve_requirement_source(source, requirements)
            if resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError('ambiguous document rename: ' + path)
            if resolution.path:
                selected.add(resolution.path)
    return selected


def workspace_evidence(snapshot, paths):
    """Bind the untouched code/index and operation without storing a new state owner."""
    return json.loads(json.dumps({
        'head': snapshot.head, 'branch': snapshot.branch,
        'index': {path: entries for path, entries in snapshot.index.items() if path not in paths},
        'dirty': {path: value for path, value in snapshot.dirty.items() if path not in paths},
        'operations': snapshot.operations,
    }))


def adopted_originals(checkpoint):
    """Keep every adopted path, including earlier replay positions."""
    item = (checkpoint.snapshot or {}).get('rebase_documents') or {}
    originals = {}
    for record in [*item.get('history', []), item]:
        if (record.get('state') not in {'prepared', 'staged'}
                or record.get('target') != checkpoint.target_base_oid
                or record.get('before_head') != checkpoint.expected_head):
            continue
        for path in record['paths']:
            if not document_path(path):
                raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'document resolution contains foreign paths')
            originals[path] = record['originals'][path]['base']
    return originals


def validate_final(*, context, checkpoint, git_bytes):
    originals = adopted_originals(checkpoint)
    if not originals:
        return
    if git_bytes is None:
        raise RecoveryFailure(RecoveryReason.STATUS_FAILED, 'document original byte reader unavailable')
    actual, _ = rebase_file_evidence.capture_originals(context.worktree, tuple(sorted(originals)),
                                                      {'head': 'HEAD'}, git_bytes)
    for path, desired in originals.items():
        content = {key: value for key, value in desired.items() if key != 'oid'}
        index = {} if desired.get('absent') else {'0': desired}
        if (actual[path]['head'] != desired or actual[path]['index'] != index
                or actual[path]['worktree'] != content):
            raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID,
                                  'replayed document differs from pinned original: ' + path)


def _save(checkpoints, identity, context, item, key="rebase_documents"):
    current = checkpoints.get(identity.repo, context.plan.plan_id)
    if (current.operation != 'rebase' or current.target_base_oid != item['target']
            or current.expected_head != item['before_head']):
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'document resolution operation changed')
    return checkpoints.record(identity.repo, context.plan.plan_id,
        expected_generation=current.generation, operation=current.operation,
        expected_head=current.expected_head, target_base_oid=current.target_base_oid,
        observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), key: item})


def resolve(*, context, identity, checkpoints, git, git_bytes, require_owned,
            capture_snapshot, lookup_pr, failed_files=None, failure_reason=None):
    """Preserve bytes before any write; never switch the owned operation target."""
    current = checkpoints.get(identity.repo, context.plan.plan_id)
    conflicts = {path for path, _, _ in parse_unmerged(git(context.worktree, 'ls-files', '-u', '-z').stdout)}
    key = 'rebase_files' if failed_files is not None else 'rebase_documents'
    allowed = _safe_path if failed_files is not None else document_path
    item = (current.snapshot or {}).get(key)
    replay = git(context.worktree, 'rev-parse', '--verify', 'REBASE_HEAD', check=False)
    position = replay.stdout.strip() if replay.returncode == 0 else (current.snapshot or {}).get('stash_oid')
    if not position:
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, '복구할 충돌 원형을 확인할 수 없습니다.')
    same = bool(item and item['position'] == position and item['target'] == current.target_base_oid
                and item['before_head'] == current.expected_head)
    if not any(allowed(path) for path in conflicts) and not (same and item['state'] != 'staged'):
        return False
    if git_bytes is None:
        raise RecoveryFailure(RecoveryReason.STATUS_FAILED, 'document original byte reader unavailable')
    if same and item['state'] == 'uncertain':
        latest = git(identity.repo_path, 'rev-parse', '--verify', f'origin/{identity.base}^{{commit}}').stdout.strip()
        if latest == item['revision']:
            raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, item['reason'])
        same = False
    require_owned()  # includes live PR identity/head, lease and operation ownership
    if not same or item['state'] == 'staged':
        revision = ((item['revision'] if same else (current.snapshot or {}).get('fetched_base_oid') or current.target_base_oid) if failed_files is not None else (item['revision'] if item and item['state'] in {'prepared', 'staged'} and item['target'] == current.target_base_oid
                    and item['before_head'] == current.expected_head else
                    git(identity.repo_path, 'rev-parse', '--verify', f'origin/{identity.base}^{{commit}}').stdout.strip()))
        if failed_files is None and git(identity.repo_path, 'merge-base', '--is-ancestor', current.target_base_oid, revision, check=False).returncode:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'document base is not a successor of the owned target')
        effects = catalog_service.RevisionExecplanSnapshotEffects(git=git, git_bytes=git_bytes)
        history = [*(item or {}).get('history', []),
                   *([{key: value for key, value in item.items() if key != 'history'}] if item else [])]
        if failed_files is not None:
            paths = set(failed_files) & conflicts
            if not paths or any(not _safe_path(path) for path in paths):
                raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, '복구 파일 범위가 유효하지 않습니다.')
            original_catalog = None
        else:
            try:
                catalog = catalog_service.revision_execplan_snapshot(effects, identity.repo_path, revision)
                requirements = revision_requirement_documents(git_bytes, identity.repo_path, revision)
                original_requirements = revision_requirement_documents(git_bytes, identity.repo_path, current.expected_head)
                original_catalog = catalog_service.revision_execplan_snapshot(effects, identity.repo_path, current.expected_head)
                paths = select_paths(conflicts, catalog, requirements, original_catalog)
                for path in paths:
                    if path.startswith('docs/requirements/'):
                        original = requirements.get(path, original_requirements.get(path))
                        if original is None:
                            raise RuntimeError('Requirement identity unavailable: ' + path)
                        requirement_identity(original.decode('utf-8'))
                active = catalog.active.get(context.plan.plan_id)
                if active is None or active.path != context.plan.path:
                    raise RuntimeError('current task deleted, archived or renamed; catalog reconciliation required')
            except (RuntimeError, ValueError) as error:
                _save(checkpoints, identity, context, {
                    'state': 'uncertain', 'position': position, 'revision': revision,
                    'target': current.target_base_oid, 'before_head': current.expected_head,
                    'reason': str(error), 'paths': sorted(path for path in conflicts if document_path(path)),
                    'history': history,
                }, key)
                raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, str(error)) from error
        paths = tuple(sorted(paths))
        revisions = {'before': current.expected_head, 'base': revision, 'replay': position, 'head': 'HEAD'}
        originals, blobs = rebase_file_evidence.capture_originals(context.worktree, paths, revisions, git_bytes,
                                                                allow_incomplete=failed_files is not None)
        before = capture_snapshot(context.worktree)
        # All affected ExecPlan PR boundaries are observed, including PRs not
        # yet projected into SQLite. Base documents are input, never Agent success.
        prs = {}
        for entry in (original_catalog.all.values() if original_catalog else ()):
            if entry.path not in paths:
                continue
            row = checkpoints.connection.execute('SELECT branch, pr_url FROM exec_plans WHERE repo = ? AND plan_id = ?',
                (identity.repo, entry.plan_id)).fetchone()
            branch = str(row['branch']) if row and row['branch'] else f'loop-manager/{entry.plan_id}'
            prs[branch] = lookup_pr(identity.repo_path, branch, str(row['pr_url'] or '') or None if row else None)
        if rebase_file_evidence.capture_originals(context.worktree, paths, revisions, git_bytes,
                                                allow_incomplete=failed_files is not None) != (originals, blobs):
            raise RuntimeError('document originals changed during preservation')
        if capture_snapshot(context.worktree) != before:
            raise RuntimeError('workspace changed during document preservation')
        item = {'state': 'prepared', 'position': position, 'revision': revision,
            'target': current.target_base_oid, 'before_head': current.expected_head,
            'paths': list(paths), 'originals': originals,
            'blobs': {digest: base64.b64encode(content).decode('ascii') for digest, content in blobs.items()},
            'prs': prs, 'workspace': workspace_evidence(before, paths), 'history': history,
            **({'failure_reason': failure_reason} if failed_files is not None else {})}
        _save(checkpoints, identity, context, item, key)
    if item['state'] == 'uncertain':
        raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, item['reason'])
    # A prepared intent survives remote base advancement. Recheck live ownership
    # and the PR boundary, but read bytes from its saved immutable revision.
    for branch, expected in item['prs'].items():
        if lookup_pr(identity.repo_path, branch, expected.get('url') if expected else None) != expected:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'document PR boundary changed')
    require_owned()
    if workspace_evidence(capture_snapshot(context.worktree), item['paths']) != item['workspace']:
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'workspace changed outside document resolution')
    if not item['paths'] or any(not allowed(path) for path in item['paths']):
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'document resolution contains foreign paths')
    originals = item['originals']
    now, _ = rebase_file_evidence.capture_originals(context.worktree, tuple(item['paths']),
                                                 {'base': item['revision']}, git_bytes,
                                                 allow_incomplete=failed_files is not None)
    skipped = set(item.get('skipped_user_changes', ()))
    for path in item['paths']:
        desired = originals[path]['base']
        actual = now[path]
        if actual['base'] != desired:
            raise RuntimeError('pinned document original differs')
        if actual['worktree'] not in (originals[path]['worktree'], desired):
            # Tree entries carry OIDs; worktree entries do not.
            if {k: v for k, v in actual['worktree'].items() if k != 'oid'} != {k: v for k, v in desired.items() if k != 'oid'}:
                if failed_files is not None:
                    skipped.add(path)
                    continue
                raise RuntimeError('document edited after preserved resolution: ' + path)
        stages = actual['index']
        expected_index = {} if desired.get('absent') else {'0': desired}
        if stages not in (originals[path]['index'], expected_index):
            if failed_files is not None:
                skipped.add(path)
                continue
            raise RuntimeError('document index changed after preservation: ' + path)
    contents = {}
    for path in item['paths']:
        if path in skipped:
            continue
        desired = originals[path]['base']
        if not desired.get('absent'):
            content = base64.b64decode(item['blobs'][desired['blob']], validate=True)
            if hashlib.sha256(content).hexdigest() != desired['blob']:
                raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'preserved document bytes differ: ' + path)
            contents[path] = content
    for path in item['paths']:
        if path in skipped:
            continue
        desired = originals[path]['base']
        target = context.worktree / path
        if desired.get('absent'):
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            if desired['mode'] == '120000':
                target.symlink_to(contents[path].decode('utf-8', 'surrogateescape'))
            else:
                target.write_bytes(contents[path])
                target.chmod(0o755 if desired['mode'] == '100755' else 0o644)
        # Cache the pinned blob itself, bypassing clean filters and platform
        # mode inference. No other path or index entry is selected.
        arguments = ('--force-remove', '--', path) if desired.get('absent') else (
            '--add', '--cacheinfo', desired['mode'], desired['oid'], path)
        result = git(context.worktree, '--literal-pathspecs', 'update-index', *arguments, check=False)
        if result.returncode:
            raise RecoveryFailure(RecoveryReason.STAGE_FAILED, result.stdout)
    for branch, expected in item['prs'].items():
        if lookup_pr(identity.repo_path, branch, expected.get('url') if expected else None) != expected:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'document PR boundary changed during application')
    applied, _ = rebase_file_evidence.capture_originals(context.worktree, tuple(item['paths']), {}, git_bytes)
    for path in item['paths']:
        if path in skipped:
            continue
        desired = originals[path]['base']
        content = {key: value for key, value in desired.items() if key != 'oid'}
        index = {} if desired.get('absent') else {'0': desired}
        if applied[path]['worktree'] != content or applied[path]['index'] != index:
            raise RecoveryFailure(RecoveryReason.STAGE_FAILED, 'pinned document application differs: ' + path)
    if workspace_evidence(capture_snapshot(context.worktree), item['paths']) != item['workspace']:
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, 'workspace changed during document resolution')
    item = {**item, 'state': 'staged', **({'skipped_user_changes': sorted(skipped)} if skipped else {})}
    _save(checkpoints, identity, context, item, key)
    require_owned()
    return True
