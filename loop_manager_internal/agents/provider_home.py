"""Provider 인증을 보관할 비공개 디렉터리 경계."""
import os
from pathlib import Path
import stat
import sys


def _owned_directory(path: Path) -> None:
    if os.name == 'nt' and sys.version_info < (3, 13):
        raise RuntimeError('Windows의 비공개 runtime 디렉터리 생성에는 Python 3.13 이상이 필요합니다.')
    path.mkdir(mode=0o700, exist_ok=True)
    status = path.lstat()
    if (not stat.S_ISDIR(status.st_mode) or path.resolve() != path.absolute()
            or path.is_symlink() or getattr(path, 'is_junction', lambda: False)()):
        raise RuntimeError('Codex runtime 경로의 소유권·권한을 확인할 수 없습니다.')
    if os.name == 'nt':
        from .windows_runtime import assert_private_directory
        assert_private_directory(path)
    elif status.st_uid != os.getuid() or status.st_mode & 0o077:
        raise RuntimeError('Codex runtime 경로의 소유권·권한을 확인할 수 없습니다.')
