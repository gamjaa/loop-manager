"""Content-free evidence for non-overlapping autostash file merges."""
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import stat
import subprocess


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
    if any(left != right and left[0] <= right[1] and right[0] <= left[1]
           for left in local_edits for right in base_edits):
        return False
    combined = local_edits + [edit for edit in base_edits if edit not in local_edits]
    expected = list(base)
    for start, end, replacement in sorted(combined, reverse=True):
        expected[start:end] = replacement
    return expected == actual
