"""Isolated, content-addressed preservation; never a publication approval.

An orphan diagnostic commit contains only inspected artifacts. Plumbing uses a
private index under the Manager state directory, leaving HEAD, refs and the
original index/rebase untouched. Durable intents precede any network write.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Callable

from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.git.manager_git_journal import _sync_directory
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage.blocked_checkpoint_state import DiagnosticPublicationRepository


@dataclass(frozen=True)
class PreservationSource:
    repo: str
    scope: str
    owner: str
    cause_identity: str
    branch: str
    worktree: Path
    revisions: dict
    detail: dict
    allowed: Callable[[str], bool]
    validate: Callable[[], object]


@dataclass(frozen=True)
class PreservationEffects:
    git: Callable
    git_bytes: Callable
    push_diagnostic_checkpoint: Callable


def artifact_observation(service):
    """Reuse current ownership policy with PR publication status kept separate.

    A closed PR/deleted original ref may still have inspectable local artifacts.
    This returns observations only, never a rebase/publication acceptance.
    """
    from loop_manager_internal.git.managed_rebase_observation import observe_current_rebase
    from loop_manager_internal.git.managed_rebase_validation import (
        ObservationFailure, RebaseStatus, assess_current_rebase, assess_scope)
    observed, scope = observe_current_rebase(service.expected, service.adapters)
    if isinstance(observed, ObservationFailure) or scope is None or not scope.allowed:
        raise RuntimeError('진단 자료의 현재 작업 범위를 확인할 수 없습니다.')
    expected_remote, actual = service.expected.remote, observed.remote
    if (actual is None or (actual.repository, actual.branch, actual.oid) !=
            (expected_remote.repository, expected_remote.branch, expected_remote.oid)):
        raise RuntimeError('진단 자료의 원래 원격 ref가 변경됐습니다.')
    expected_pr, actual_pr = expected_remote.pull_request, actual.pull_request
    if bool(expected_pr) != bool(actual_pr) or actual_pr and (
            (actual_pr.identity, actual_pr.repository, actual_pr.branch) !=
            (expected_pr.identity, expected_pr.repository, expected_pr.branch)
            or actual_pr.state not in {'OPEN', 'CLOSED', 'MERGED'}
            or actual.oid is not None and actual_pr.head != actual.oid):
        raise RuntimeError('진단 자료의 저장된 PR identity가 다릅니다.')
    expected = replace(service.expected, remote=replace(expected_remote, pull_request=None))
    candidate = replace(observed, remote=replace(actual, pull_request=None))
    scoped = assess_scope(expected, candidate, path_allowed=service.adapters.path_allowed,
                          contracts_allowed=service.adapters.contracts_allowed)
    decision = assess_current_rebase(expected, candidate, scoped)
    if decision.status not in {RebaseStatus.START, RebaseStatus.WAITING, RebaseStatus.COMPLETE}:
        raise RuntimeError('진단 자료의 worktree·rebase 소유권이 유효하지 않습니다: ' + decision.reason)
    return observed


_PRIVATE = re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|'
                      r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b')


def redact_detail(value: str) -> str:
    clean = redact_secrets(value)
    clean = re.sub(r'(?s)-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?'
                   r'(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|$)', '<REDACTED>', clean)
    clean = re.sub(r'(?i)(https?://)[^/\s@]+@', r'\1<REDACTED>@', clean)
    return _PRIVATE.sub('<REDACTED>', clean)


def sensitive(content: bytes) -> bool:
    # Reuse the feedback credential scrubber, including UTF-16 and binary
    # ASCII strings; never redact a blob and claim it is an original.
    for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
        text = content.decode(encoding, errors='ignore')
        if redact_detail(text) != text:
            return True
    return False


def _private_path(name):
    return any(part.casefold() in {'.env', '.aws', '.ssh', '.codex', '.claude',
                                  'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
               or part.casefold().endswith(('.pem', '.key'))
               or part.casefold().startswith('.env.') for part in Path(name).parts)


def collect(effects, source, snapshot):
    """Select changed paths only, keeping excluded material out of every diff."""
    paths = set(snapshot.dirty)
    paths.update(name for name, entries in snapshot.index.items()
                 if any(entry.split()[-1] != '0' for entry in entries))
    for revision in source.detail.get('selection_revisions', [source.revisions['head']]):
        raw = effects.git_bytes(source.worktree, 'diff', '--no-ext-diff', '--no-textconv',
                                '--name-only', '--no-renames', '-z',
                                source.detail.get('selection_base', source.revisions['base']), revision)
        paths.update(name.decode('utf-8', 'surrogateescape') for name in raw.split(b'\0') if name)
    if source.detail.get('untracked_revision'):
        raw = effects.git_bytes(source.worktree, 'ls-tree', '-r', '--name-only', '-z', source.detail['untracked_revision'])
        paths.update(name.decode('utf-8', 'surrogateescape') for name in raw.split(b'\0') if name)
    records, blobs, omitted = {}, {}, {}
    for name in sorted(paths):
        if not source.allowed(name) or _private_path(name):
            omitted[name] = 'outside verified scope or private path'
            continue
        ignored = effects.git(source.worktree, '--literal-pathspecs', 'check-ignore',
                              '--no-index', '--quiet', '--', name, check=False)
        if ignored.returncode != 1:
            omitted[name] = 'ignored path or ignore inspection unavailable'
            continue
        try:
            item, contents = rebase_file_evidence.capture_originals(
                source.worktree, (name,), source.revisions, effects.git_bytes)
        except (OSError, RuntimeError, ValueError):
            omitted[name] = 'original bytes or mode unavailable'
            continue
        if any(sensitive(content) for content in contents.values()):
            omitted[name] = 'credential content'
            continue
        records.update(item)
        blobs.update(contents)
    assets = {'blobs/' + key: value for key, value in blobs.items()}
    if records:
        for label, args in (
            ('worktree', (source.revisions['base'],)),
            ('staged', ('--cached', 'HEAD')),
            ('unstaged', ()),
        ):
            diff = effects.git_bytes(source.worktree, '-c', 'diff.autoRefreshIndex=false', '--literal-pathspecs', 'diff',
                                     '--no-ext-diff', '--no-textconv', '--binary',
                                     '--no-renames', *args, '--', *sorted(records))
            if sensitive(diff):
                omitted['diff/' + label] = 'credential content'
            else:
                assets['diff/' + label + '.patch'] = diff
    manifest = dict(version=1, scope=source.scope, owner=source.owner,
                    original_branch=source.branch, revisions=source.revisions,
                    detail=source.detail, files=records, omitted=omitted,
                    complete=not omitted,
                    remaining='Resolve the recorded cause on the original branch/base; '
                              'artifacts are evidence only. Index stages 1/2/3 are base/ours/theirs.')
    rendered = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2).encode()
    if sensitive(rendered):
        raise RuntimeError('진단 manifest에 비밀값이 있어 게시하지 않습니다.')
    assets['manifest.json'] = rendered
    return assets, manifest


def _commit(effects, state_dir, source, assets):
    directory = state_dir / 'diagnostic-artifacts'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        root = Path(temporary)
        environment = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
        environment.update(GIT_INDEX_FILE=str(root / 'index'), GIT_CONFIG_NOSYSTEM='1',
                           GIT_AUTHOR_NAME='Loop Manager', GIT_COMMITTER_NAME='Loop Manager',
                           GIT_AUTHOR_EMAIL='loop-manager@localhost',
                           GIT_COMMITTER_EMAIL='loop-manager@localhost',
                           GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00',
                           GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00')
        def git(*args):
            return effects.git(source.worktree, '-c', 'core.hooksPath=' + os.devnull,
                               '-c', 'commit.gpgSign=false', *args, env=environment).stdout.strip()
        git('read-tree', '--empty')
        for name, content in sorted(assets.items()):
            file = root / 'blob'
            file.write_bytes(content)
            oid = git('hash-object', '-w', '--no-filters', str(file))
            git('update-index', '--add', '--cacheinfo', '100644', oid, name)
        tree = git('write-tree')
        # No parent: unrelated repository contents/history cannot become reachable.
        head = git('commit-tree', tree, '-m', 'Loop Manager diagnostic artifacts')
    return tree, head


def _save_assets(state_dir, identity, assets):
    """Retain inspected bytes across object GC and interrupted commit/push."""
    directory = state_dir / 'diagnostic-artifacts'
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / identity
    if bundle.exists():
        if bundle.is_symlink() or any((bundle / name).is_symlink()
                                     or (bundle / name).read_bytes() != data for name, data in assets.items()):
            raise RuntimeError('관리 진단 자료의 identity가 변경됐습니다.')
        return
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        root = Path(temporary)
        for name, data in assets.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        for subdirectory in (root / 'blobs', root / 'diff', root):
            if subdirectory.exists():
                _sync_directory(subdirectory)
        os.replace(root, bundle)
        _sync_directory(directory)


def preserve(effects, connection, state_dir, source, adapter, request, original_reason):
    store = DiagnosticPublicationRepository(connection)
    snapshot = source.validate()
    assets, manifest = collect(effects, source, snapshot)
    identity = hashlib.sha256(json.dumps(
        [source.cause_identity, snapshot.serialize(),
         {name: hashlib.sha256(data).hexdigest() for name, data in sorted(assets.items())}],
        sort_keys=True).encode()).hexdigest()
    saved = store.preservation(source.repo, source.scope, source.owner, identity)
    if saved and (json.loads(saved['payload']).get('result') == 'published' or not saved['retryable']):
        payload = json.loads(saved['payload'])
        store.preserve(source.repo, source.scope, source.owner, identity, payload,
                       ref=saved['ref'], tree=saved['tree_oid'], commit=saved['commit_oid'])
        return payload
    tree = head = ref = None
    payload = dict(result='local_only', diagnostic=True, original_branch=source.branch,
                   worktree=str(source.worktree), cause=source.detail,
                   cause_identity=source.cause_identity, paths=sorted(manifest['files']),
                   omitted=manifest['omitted'], complete=manifest['complete'],
                   original_unpublished_reason=redact_detail(original_reason))
    def unchanged():
        current = source.validate()
        if (current.serialize() != snapshot.serialize()
                or collect(effects, source, current)[0] != assets):
            raise RuntimeError('진단 수집 중 원본 작업물 또는 소유권이 변경됐습니다.')
    def validate_receipt():
        if source.validate().serialize() != snapshot.serialize():
            raise RuntimeError('진단 게시 intent 또는 확정 중 원본이 변경됐습니다.')
    try:
        unchanged()
        adapter._validated_push_url(request)
        _save_assets(state_dir, identity, assets)
        payload['artifact_path'] = str(state_dir / 'diagnostic-artifacts' / identity)
        if saved and saved['commit_oid']:
            tree, head, ref = saved['tree_oid'], saved['commit_oid'], saved['ref']
            available = effects.git(source.worktree, 'rev-parse', '--verify', head + '^{tree}', check=False)
            if available.returncode:
                if _commit(effects, state_dir, source, assets) != (tree, head):
                    raise RuntimeError('진단 commit 재구성 identity가 다릅니다.')
            elif available.stdout.strip() != tree:
                raise RuntimeError('진단 commit tree가 저장 intent와 다릅니다.')
        else:
            tree, head = _commit(effects, state_dir, source, assets)
            ref = blocked_checkpoint.diagnostic_branch(
                source.repo, source.owner, source.branch, source.revisions['base'], head, tree)
        payload.update(branch=ref, commit_oid=head, base_oid=source.revisions['base'])
        store.preserve(source.repo, source.scope, source.owner, identity, payload,
                       ref=ref, tree=tree, commit=head, retryable=True, revalidate=validate_receipt)
        # Diagnostic observation must not query/recreate an existing PR.
        target = replace(request, branch=ref, pr_url=None)
        remote = adapter.observe_remote(target)
        decision = blocked_checkpoint.decide_diagnostic_push(head, remote)
        if decision.mode is blocked_checkpoint.PushMode.REJECT:
            raise blocked_checkpoint.CheckpointPushError(decision.reason, retryable=False)
        unchanged()
        if decision.mode is blocked_checkpoint.PushMode.CREATE:
            effects.push_diagnostic_checkpoint(adapter, target, head)
        if adapter.observe_remote(target) != head:
            raise blocked_checkpoint.CheckpointPushError('진단 ref OID를 확인할 수 없습니다.', retryable=True)
        unchanged()
        payload.update(result='published', reason=None)
        store.preserve(source.repo, source.scope, source.owner, identity, payload,
                       ref=ref, tree=tree, commit=head, revalidate=validate_receipt)
    except (OSError, RuntimeError, sqlite3.Error) as error:
        payload['result'] = 'local_only'
        payload['reason'] = redact_detail(str(error))
        store.preserve(source.repo, source.scope, source.owner, identity, payload,
                       ref=ref, tree=tree, commit=head,
                       retryable=not isinstance(error, blocked_checkpoint.CheckpointPushError) or error.retryable)
    return payload
