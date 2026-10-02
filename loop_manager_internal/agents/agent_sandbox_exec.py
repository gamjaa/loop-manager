"""Standalone native tool-policy probe; never launches a provider process.

Check declared protected paths without mutating them and exercise writes only
in explicitly allowed scratch/workspace roots. No process-tree mount scan.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sys
import tempfile


class BoundaryError(RuntimeError):
    pass


def check_boundary(spec: dict) -> None:
    # The Linux native sandbox presents declared protected roots read-only.
    # Check only those roots, not unrelated mounts or workspace descendants.
    for value in spec['protected']:
        root = Path(value)
        if not root.exists() or not os.statvfs(root).f_flag & os.ST_RDONLY:
            raise BoundaryError('protected path is not read-only')
    for value in spec['files']:
        try:
            descriptor = os.open(value, os.O_WRONLY | os.O_CLOEXEC)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise BoundaryError('protected file probe was inconclusive') from error
        else:
            os.close(descriptor)
            raise BoundaryError('protected file write was allowed')
    for value in spec['writable']:
        with tempfile.TemporaryDirectory(prefix='.isolation-', dir=value) as scratch:
            target = Path(scratch) / 'probe'
            target.write_bytes(b'probe')
            target.write_bytes(b'updated')
            replacement = target.with_name('renamed')
            target.rename(replacement)
            replacement.unlink()


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise BoundaryError('probe cannot launch a command')
        check_boundary(json.loads(sys.argv[1]))
    except (BoundaryError, OSError, ValueError, KeyError, TypeError):
        print('agent tool boundary verification failed', file=sys.stderr)
        return 125
    print('agent-tool-boundary-v2')
    return 0


if __name__ == '__main__':
    sys.exit(main())
