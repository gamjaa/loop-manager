"""Content-free evidence for non-overlapping autostash file merges."""
from difflib import SequenceMatcher
import hashlib
import os
from pathlib import Path
import stat
import subprocess


def stash_originals(worktree: Path, stash: str, git) -> dict[str, str]:
    """Resolve a proven stash and its original index without applying it."""
    def revision(expression):
        result = git(worktree, 'rev-parse', '--verify', expression, check=False)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('diagnostic stash original revision unavailable')
        return result.stdout.strip()
    oid = revision(stash + '^{commit}')
    revisions = {'stash': oid, 'stash-base': revision(oid + '^1'),
                 'staged-original': revision(oid + '^2')}
    untracked = git(worktree, 'rev-parse', '--verify', oid + '^3', check=False)
    if untracked.returncode == 0:
        if not untracked.stdout.strip():
            raise RuntimeError('diagnostic stash untracked revision unavailable')
        revisions['untracked-original'] = untracked.stdout.strip()
    elif untracked.returncode not in (1, 128):
        raise RuntimeError('diagnostic stash untracked revision unavailable')
    return revisions


def capture_originals(worktree: Path, paths: tuple[str, ...], revisions: dict,
                      git_bytes, *, allow_incomplete=False) -> tuple[dict, dict[str, bytes]]:
    """Read exact tree/index/worktree bytes without staging or resolving files.

    The caller validates ownership, paths and secrets, then repeats collection
    to reject concurrent edits. Absence is explicit; gitlinks are incomplete.
    """
    from pathlib import PurePosixPath
    records, blobs = {}, {}

    def store(mode, content, oid=None):
        digest = hashlib.sha256(content).hexdigest()
        blobs[digest] = content
        return {'mode': mode, 'blob': digest, **({'oid': oid} if oid else {})}

    for name in paths:
        relative = PurePosixPath(name)
        if (relative.is_absolute() or Path(name).is_absolute() or '..' in relative.parts or '\\' in name
                or not name or '\0' in name or any(part.casefold() == '.git' for part in relative.parts)
                or os.name == 'nt' and ':' in name):
            raise RuntimeError('unsafe diagnostic path')
        target = worktree / name
        # A symlink parent must never turn a selected path into an external read.
        if any(parent.is_symlink() for parent in target.parents
               if parent != worktree and worktree in parent.parents):
            raise RuntimeError('diagnostic symlink path')
        item = {}
        for label, revision in revisions.items():
            raw = git_bytes(worktree, '--literal-pathspecs', 'ls-tree', '-z',
                            revision, '--', name)
            entries = [entry for entry in raw.split(b'\0') if entry]
            if not entries:
                item[label] = {'absent': True}
                continue
            if len(entries) != 1:
                raise RuntimeError('ambiguous diagnostic tree entry')
            metadata, actual = entries[0].split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split()
            if actual != name.encode('utf-8', 'surrogateescape') or kind != 'blob':
                raise RuntimeError('unsupported diagnostic tree entry')
            try:
                item[label] = store(mode, git_bytes(worktree, 'cat-file', 'blob', oid), oid)
            except RuntimeError:
                if not allow_incomplete or label == 'base':
                    raise
                item[label] = {'mode': mode, 'oid': oid, 'unavailable': 'blob'}
        stages = {}
        raw = git_bytes(worktree, '--literal-pathspecs', 'ls-files', '--stage', '-z', '--', name)
        for entry in filter(None, raw.split(b'\0')):
            metadata, actual = entry.split(b'\t', 1)
            mode, oid, stage = metadata.decode('ascii').split()
            if (actual != name.encode('utf-8', 'surrogateescape')
                    or stage in stages or mode not in {'100644', '100755', '120000'}):
                raise RuntimeError('unsupported diagnostic index entry')
            try:
                stages[stage] = store(mode, git_bytes(worktree, 'cat-file', 'blob', oid), oid)
            except RuntimeError:
                if not allow_incomplete:
                    raise
                stages[stage] = {'mode': mode, 'oid': oid, 'unavailable': 'blob'}
        item['index'] = stages
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            item['worktree'] = {'absent': True}
        else:
            if stat.S_ISLNK(mode):
                item['worktree'] = store('120000', os.fsencode(os.readlink(target)))
                records[name] = item
                continue
            if not stat.S_ISREG(mode):
                raise RuntimeError('unsupported diagnostic worktree entry')
            file_mode = '100755' if mode & stat.S_IXUSR else '100644'
            if os.name == 'nt':
                file_mode = stages.get('0', {}).get('mode', '100644')
            try:
                item['worktree'] = store(file_mode, target.read_bytes())
            except OSError:
                if not allow_incomplete:
                    raise
                item['worktree'] = {'mode': file_mode, 'unavailable': 'bytes'}
        records[name] = item
    return records, blobs


def capture(worktree: Path, head: str, dirty: dict) -> dict:
    evidence = {}
    for name, (kinds, signature) in dirty.items():
        if 'untracked' in kinds:
            continue
        path = worktree / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                continue
            contents = path.read_bytes()
        except OSError:
            continue
        actual = hashlib.sha256(str(mode).encode('ascii') + b'\0' + contents).hexdigest()
        if actual != signature or b'\0' in contents:
            continue
        tree = subprocess.run(
            ['git', '-C', str(worktree), 'ls-tree', '-z', head, '--', name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if tree.returncode or not tree.stdout.startswith((b'100644 blob ', b'100755 blob ')):
            continue
        blob = tree.stdout.split(b'\t', 1)[0].split()[-1].decode('ascii')
        base = subprocess.run(
            ['git', '-C', str(worktree), 'cat-file', 'blob', blob],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if base.returncode or b'\0' in base.stdout:
            continue
        evidence[name] = {
            'signature': signature, 'mode': mode,
            'base_mode': tree.stdout.split()[0].decode('ascii'),
            'base': [hashlib.sha256(line).hexdigest() for line in base.stdout.splitlines(True)],
            'local': [hashlib.sha256(line).hexdigest() for line in contents.splitlines(True)],
        }
    return evidence


def capture_modes(worktree: Path, files: dict, path_signature) -> dict:
    """Bind filesystem types/executable bits to the already approved signatures."""
    modes = {}
    for name, signature in files.items():
        try:
            mode = (worktree / name).lstat().st_mode
        except FileNotFoundError:
            approved = None
        else:
            if stat.S_ISREG(mode):
                approved = '100755' if mode & stat.S_IXUSR else '100644'
            elif stat.S_ISLNK(mode):
                approved = '120000'
            elif stat.S_ISDIR(mode):
                approved = '160000'
            else:
                raise RuntimeError('rebase resolution: unsupported approved file type')
        if path_signature(worktree, name) != signature:
            raise RuntimeError('rebase resolution: file changed while capturing approved mode')
        modes[name] = approved
    return modes


def preserved(before: dict, after: dict, old_signature: str, new_signature: str) -> bool:
    """Replay disjoint local/base edits on hashed lines; reject ambiguous overlap."""
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or before.get('signature') != old_signature
            or after.get('signature') != new_signature
            or before.get('mode') != after.get('mode')
            or before.get('base_mode') != after.get('base_mode')):
        return False
    sequences = [before.get('base'), before.get('local'), after.get('base'), after.get('local')]
    if any(not isinstance(lines, list) or any(not isinstance(x, str) for x in lines)
           for lines in sequences):
        return False
    base, local, new_base, actual = sequences

    def edits(target):
        return [(i, j, target[a:b]) for tag, i, j, a, b in
                SequenceMatcher(None, base, target, autojunk=False).get_opcodes()
                if tag != 'equal']

    local_edits, base_edits = edits(local), edits(new_base)
    def overlaps(left, right):
        if left == right:
            return False
        # Replacements/deletions occupy half-open ranges, so adjacent edits
        # are independent. Insertions have no width: keep shared insertion
        # points and replacement boundaries conservative because order matters.
        if left[0] == left[1] or right[0] == right[1]:
            return left[0] <= right[1] and right[0] <= left[1]
        return left[0] < right[1] and right[0] < left[1]

    if any(overlaps(left, right) for left in local_edits for right in base_edits):
        return False
    combined = local_edits + [edit for edit in base_edits if edit not in local_edits]
    expected = list(base)
    for start, end, replacement in sorted(combined, reverse=True):
        expected[start:end] = replacement
    return expected == actual
