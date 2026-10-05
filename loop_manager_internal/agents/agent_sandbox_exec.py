"""Standalone native tool-policy probe; never launches a provider process.

Check declared protected paths without mutating them and exercise writes only
in explicitly allowed scratch/workspace roots. No process-tree mount scan.
"""
from __future__ import annotations

import errno
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile


class BoundaryError(RuntimeError):
    pass


def _windows_readonly_directory(root):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # OPEN_EXISTING으로 권한만 요청한다. 파일 생성·삭제·내용 변경은 없다.
    for access in (2, 4, 0x40, 0x100, 0x10000):
        handle = kernel.CreateFileW(str(root), access, 7, None, 3, 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() not in (5, 1314):
                raise BoundaryError('protected directory probe was inconclusive')
        else:
            kernel.CloseHandle(handle)
            raise BoundaryError('protected directory write was allowed')


def _darwin_readonly_path(root):
    library = ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
    check = library.sandbox_check
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    check.restype = ctypes.c_int
    flags = 1 | ctypes.c_int.in_dll(library, 'SANDBOX_CHECK_NO_REPORT').value
    for operation in (b'file-write-data', b'file-write-create', b'file-write-unlink', b'file-write-mode'):
        if check(os.getpid(), operation, flags, ctypes.c_char_p(os.fsencode(root))) != 1:
            raise BoundaryError('protected path write denial was not confirmed')


def check_boundary(spec: dict) -> None:
    # Linux는 read-only mount, Windows/macOS는 파일 접근 정책을 검사한다.
    for value in spec['protected']:
        root = Path(value)
        if not root.exists():
            raise BoundaryError('protected path is not read-only')
        if sys.platform.startswith('linux'):
            if not os.statvfs(root).f_flag & os.ST_RDONLY:
                raise BoundaryError('protected path is not read-only')
        elif sys.platform == 'darwin':
            _darwin_readonly_path(root)
        elif sys.platform == 'win32' and root.is_dir():
            _windows_readonly_directory(root)
        elif sys.platform != 'win32':
            raise BoundaryError('unsupported sandbox platform')
    files = dict.fromkeys([*spec['files'], *(value for value in spec['protected'] if Path(value).is_file())])
    for value in files:
        try:
            descriptor = os.open(value, os.O_WRONLY | getattr(os, 'O_CLOEXEC', 0))
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
