"""Isolated, content-addressed preservation; never a publication approval.

An orphan diagnostic commit contains only inspected artifacts. Plumbing uses a
private index under the Manager state directory, leaving HEAD, refs and the
original index/rebase untouched. Durable intents precede any network write.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tempfile
from typing import Callable

from loop_manager_internal.runtime.resource_lock import resource_locks
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
    pairs = [(source.detail.get('selection_base', source.revisions['base']), revision)
             for revision in source.detail.get('selection_revisions', [source.revisions['head']])]
    if 'stash-base' in source.revisions:
        pairs.extend((source.revisions['stash-base'], source.revisions[label])
                     for label in ('stash', 'staged-original'))
    for base, revision in pairs:
        raw = effects.git_bytes(source.worktree, 'diff', '--no-ext-diff', '--no-textconv',
                                '--name-only', '--no-renames', '-z',
                                base, revision)
        paths.update(name.decode('utf-8', 'surrogateescape') for name in raw.split(b'\0') if name)
    if source.detail.get('untracked_revision'):
        raw = effects.git_bytes(source.worktree, 'ls-tree', '-r', '--name-only', '-z', source.detail['untracked_revision'])
        paths.update(name.decode('utf-8', 'surrogateescape') for name in raw.split(b'\0') if name)
    records, blobs, omitted = {}, {}, {}
    for name in sorted(paths):
        if not source.allowed(name) or _private_path(name):
            omitted[name] = 'outside verified scope or private path'
            continue
        # check-ignore consumes paths and rejects literal pathspec magic.
        # Disable an inherited literal setting without weakening fail-closed checks.
        ignored = effects.git(source.worktree, '--no-literal-pathspecs', 'check-ignore',
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
        diffs = [
            ('worktree', (source.revisions['base'],)),
            ('staged', ('--cached', 'HEAD')),
            ('unstaged', ()),
        ]
        if 'stash-base' in source.revisions:
            diffs.extend((label, (source.revisions['stash-base'], source.revisions[label]))
                         for label in ('stash', 'staged-original'))
        for label, args in diffs:
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
    if directory.is_symlink():
        raise RuntimeError('진단 자료 디렉터리가 symlink입니다.')
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
    if directory.is_symlink():
        raise RuntimeError('진단 자료 디렉터리가 symlink입니다.')
    bundle = directory / identity
    if bundle.exists() or bundle.is_symlink():
        if _read_assets(bundle) != assets:
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


def _read_assets(bundle):
    """Read regular bundle files only, rejecting redirected or changing paths."""
    def signature(path, directory=False):
        value = path.lstat()
        if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
            raise RuntimeError('진단 bundle에 일반 파일이 아닌 경로가 있습니다.')
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns,
                value.st_ctime_ns)

    roots = {bundle.parent: signature(bundle.parent, True), bundle: signature(bundle, True)}
    assets = {}
    for path in sorted(bundle.iterdir()):
        if path.name in {'blobs', 'diff'}:
            roots[path] = signature(path, True)
            files = sorted(path.iterdir())
        else:
            files = [path]
        for file in files:
            before = signature(file)
            assets[file.relative_to(bundle).as_posix()] = file.read_bytes()
            if signature(file) != before:
                raise RuntimeError('진단 bundle을 읽는 중 파일이 변경됐습니다.')
    if any(signature(path, True) != before for path, before in roots.items()):
        raise RuntimeError('진단 bundle을 읽는 중 경로가 변경됐습니다.')
    return assets


def artifact_identity(assets, algorithm):
    """Rebuild this fixed orphan format without Git objects, refs or an index."""
    def oid(kind, data):
        return hashlib.new(algorithm, kind + b' ' + str(len(data)).encode() + b'\0' + data).digest()

    groups = {'': []}
    for name, data in sorted(assets.items()):
        parent, _, leaf = name.rpartition('/')
        if parent not in {'', 'blobs', 'diff'} or not leaf or '/' in leaf:
            raise RuntimeError('진단 bundle의 자료 경로가 유효하지 않습니다.')
        groups.setdefault(parent, []).append(b'100644 ' + leaf.encode('ascii') + b'\0' + oid(b'blob', data))
    root = {entry.split(b'\0', 1)[0].split(b' ', 1)[1]: entry for entry in groups['']}
    for name in sorted(set(groups) - {''}):
        root[name.encode()] = b'40000 ' + name.encode() + b'\0' + oid(b'tree', b''.join(groups[name]))
    tree = oid(b'tree', b''.join(root[key] for key in sorted(root))).hex()
    author = b'Loop Manager <loop-manager@localhost> 946684800 +0000\n'
    commit = oid(b'commit', b'tree ' + tree.encode() + b'\nauthor ' + author +
                 b'committer ' + author + b'\nLoop Manager diagnostic artifacts\n').hex()
    return tree, commit


def load_verified_artifacts(state_dir, saved):
    """Return recoverable bytes and modes, or a preservation reason via exception.

    SQLite's immutable publication intent binds the orphan tree and commit. A
    fresh Git object database, worktree or remote ref is deliberately unnecessary.
    """
    identity = saved['input_identity']
    if not isinstance(identity, str) or re.fullmatch('[0-9a-f]{64}', identity) is None:
        raise RuntimeError('진단 bundle의 입력 identity가 유효하지 않습니다.')
    payload = json.loads(saved['payload'])
    bundle = Path(state_dir) / 'diagnostic-artifacts' / identity
    if (not isinstance(payload, dict) or payload.get('artifact_path') != str(bundle)
            or payload.get('diagnostic') is not True
            or not isinstance(payload.get('original_branch'), str) or not payload['original_branch']):
        raise RuntimeError('진단 bundle의 저장 경로 identity가 다릅니다.')
    assets = _read_assets(bundle)
    tree, commit = saved['tree_oid'], saved['commit_oid']
    if not isinstance(tree, str) or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', tree) is None:
        raise RuntimeError('진단 bundle의 저장 tree identity가 유효하지 않습니다.')
    algorithm = 'sha1' if len(tree) == 40 else 'sha256'
    if artifact_identity(assets, algorithm) != (tree, commit):
        raise RuntimeError('진단 bundle의 tree·commit identity가 다릅니다.')
    if 'manifest.json' not in assets:
        raise RuntimeError('진단 manifest가 없습니다.')
    manifest = json.loads(assets['manifest.json'])
    if (not isinstance(manifest, dict) or type(manifest.get('version')) is not int
            or manifest['version'] != 1 or manifest.get('scope') != saved['scope']
            or manifest.get('owner') != saved['owner']
            or manifest.get('original_branch') != payload.get('original_branch')
            or not isinstance(manifest.get('detail'), dict)
            or manifest.get('detail') != payload.get('cause')
            or manifest.get('complete') is not True or manifest.get('omitted') != {}
            or payload.get('complete') is not True or payload.get('omitted') != {}
            or not isinstance(manifest.get('files'), dict)
            or not isinstance(manifest.get('revisions'), dict)
            or not {'base', 'head'} <= set(manifest['revisions'])
            or not all(isinstance(key, str) and key and isinstance(value, str) and value
                       for key, value in manifest['revisions'].items())):
        raise RuntimeError('진단 manifest의 identity 또는 원문 완전성을 확인할 수 없습니다.')
    required = {'manifest.json'}
    for name, versions in manifest['files'].items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or '..' in relative.parts or '\\' in name
                or '\0' in name or any(part.casefold() == '.git' for part in relative.parts)
                or not isinstance(versions, dict)
                or set(versions) != set(manifest['revisions']) | {'index', 'worktree'}
                or not isinstance(versions['index'], dict)
                or not set(versions['index']) <= {'0', '1', '2', '3'}):
            raise RuntimeError('진단 원문 경로 또는 버전이 유효하지 않습니다.')
        originals = [value for key, value in versions.items() if key != 'index']
        originals.extend(versions['index'].values())
        for original in originals:
            if not isinstance(original, dict):
                raise RuntimeError('진단 원문의 bytes·모드가 유효하지 않습니다.')
            if set(original) == {'absent'} and original['absent'] is True:
                continue
            digest = original.get('blob')
            if (not isinstance(original.get('mode'), str)
                    or original['mode'] not in {'100644', '100755', '120000'}
                    or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                    or not set(original) <= {'mode', 'blob', 'oid'}):
                raise RuntimeError('진단 원문의 bytes·모드가 유효하지 않습니다.')
            key = 'blobs/' + digest
            if key not in assets:
                raise RuntimeError('진단 원문 blob이 없습니다.')
            data = assets[key]
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError('진단 원문 blob의 identity가 다릅니다.')
            if 'oid' in original:
                header = b'blob ' + str(len(data)).encode() + b'\0'
                if hashlib.new(algorithm, header + data).hexdigest() != original['oid']:
                    raise RuntimeError('진단 원문의 Git blob identity가 다릅니다.')
            required.add(key)
    if manifest['files']:
        labels = {'worktree', 'staged', 'unstaged'}
        if 'stash-base' in manifest['revisions']:
            labels.update({'stash', 'staged-original'})
        required.update('diff/' + label + '.patch' for label in labels)
    if set(assets) != required:
        raise RuntimeError('진단 bundle의 diff·blob·manifest 자료가 누락되거나 다릅니다.')
    if (payload.get('branch'), payload.get('commit_oid')) != (saved['ref'], commit):
        raise RuntimeError('진단 bundle의 게시 identity가 다릅니다.')
    expected_ref = blocked_checkpoint.diagnostic_branch(saved['repo'], saved['owner'],
        payload['original_branch'], manifest['revisions'].get('base'), commit, tree)
    if saved['ref'] != expected_ref or payload.get('base_oid') != manifest['revisions'].get('base'):
        raise RuntimeError('진단 bundle의 ref·base identity가 다릅니다.')
    return assets, manifest


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
        current = store.current_payload(source.repo, payload)
        if current == payload:
            store.preserve(source.repo, source.scope, source.owner, identity, payload,
                           ref=saved['ref'], tree=saved['tree_oid'], commit=saved['commit_oid'])
        return current
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
        with resource_locks(state_dir, source.repo, ['diagnostic:' + ref], owner='diagnostic-preservation'):
            if store.cleanup_receipt(source.repo, ref, source.cause_identity) is not None:
                return store.current_payload(source.repo, payload)
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
