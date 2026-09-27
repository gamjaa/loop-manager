"""Small trusted gate executed *inside* the native Codex sandbox.

No provider code runs until this gate has checked the actual mount namespace.
The same gate is used for preflight and exec; source files are never mutated.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


class BoundaryError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def mount_table(text: str) -> list[tuple[Path, bool, str]]:
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        split = fields.index('-')
        name = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        mounts.append((Path(name), 'ro' in fields[5].split(','), fields[split + 1]))
    if not mounts:
        raise BoundaryError('mount table unavailable')
    return mounts


def check_mounts(mounts: list[tuple[Path, bool, str]], writable: list[Path],
                 protected: list[Path]) -> None:
    # A Landlock-only/unknown implementation is not claimed as mount proof.
    if not any(path == Path('/') and readonly for path, readonly, _ in mounts):
        raise BoundaryError('root mount is not read-only')
    for path, readonly, filesystem in mounts:
        if readonly:
            continue
        if any(_inside(path, root) for root in writable):
            if any(_inside(path, root) for root in protected):
                raise BoundaryError('writable mount below protected path')
            continue
        # Native sandbox proc/device filesystems are not host repository storage.
        if filesystem in ('proc', 'devpts', 'tmpfs') and (
                _inside(path, Path('/proc')) or _inside(path, Path('/dev'))):
            continue
        raise BoundaryError('unexpected writable mount')
    for root in protected:
        candidates = [(path, readonly) for path, readonly, _ in mounts if _inside(root, path)]
        if not candidates or not max(candidates, key=lambda item: len(item[0].parts))[1]:
            raise BoundaryError('protected path mount is writable')
        if any(_inside(path, root) and not readonly for path, readonly, _ in mounts):
            raise BoundaryError('protected subtree has a writable mount')


def check_hardlinks(roots: list[Path]) -> None:
    # A RO bind protects a path, not an inode reachable by an older hardlink in
    # an allowed mount. Reject such aliases without reading file contents.
    def failed(_error):
        raise BoundaryError('writable tree cannot be inspected')

    for root in roots:
        for folder, _directories, files in os.walk(root, followlinks=False, onerror=failed):
            for name in files:
                metadata = (Path(folder) / name).lstat()
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                    raise BoundaryError('existing hardlink in writable root')


def check_boundary(spec: dict) -> None:
    writable = [Path(value) for value in spec['writable']]
    protected = [Path(value) for value in spec['protected']]
    check_mounts(mount_table(Path('/proc/self/mountinfo').read_text()), writable, protected)
    status = Path('/proc/self/status').read_text()
    values = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    if int(values.get('CapEff', '1').strip(), 16) or values.get('NoNewPrivs', '').strip() != '1':
        raise BoundaryError('sandbox privilege reduction is unavailable')
    # Host /proc/<pid>/root must not provide an alternate writable host mount.
    if os.readlink('/proc/1/ns/mnt') != os.readlink('/proc/self/ns/mnt'):
        raise BoundaryError('host process namespace remains reachable')
    for root in protected:
        if not root.exists() or not os.statvfs(root).f_flag & os.ST_RDONLY:
            raise BoundaryError('protected path is not read-only')
    check_hardlinks(writable)
    # Existing-file write opens are non-destructive, even if confinement failed.
    for value in spec['files']:
        try:
            descriptor = os.open(value, os.O_WRONLY | os.O_CLOEXEC)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise BoundaryError('protected file probe was inconclusive') from error
        else:
            os.close(descriptor)
            raise BoundaryError('protected file write was allowed')
    # Only Manager-owned scratch paths and the selected writing worktree are
    # touched. RO mount proof above also covers create/unlink/rename of protected
    # names, without creating a sentinel in a user's source checkout.
    for root in writable:
        with tempfile.TemporaryDirectory(prefix='.isolation-', dir=root) as scratch:
            target = Path(scratch) / 'probe'
            target.write_bytes(b'probe')
            target.write_bytes(b'updated')
            replacement = target.with_name('renamed')
            target.rename(replacement)
            replacement.unlink()


def main() -> int:
    spec = json.loads(sys.argv[1])
    try:
        check_boundary(spec)
    except (BoundaryError, OSError, ValueError, KeyError):
        # Paths, provider configuration and source data never enter diagnostics.
        print('agent write boundary verification failed', file=sys.stderr)
        return 125
    command = sys.argv[2:]
    if not command:
        print('agent-write-boundary-v1')
        return 0
    os.chdir(spec['worktree'])
    os.execvpe(command[0], command, os.environ)
    return 125


if __name__ == '__main__':
    sys.exit(main())
