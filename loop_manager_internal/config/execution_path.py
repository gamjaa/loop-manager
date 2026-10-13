"""Linux 설치별 PATH 캐시와 bootstrap 실행 환경."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

from loop_manager_internal.config.managed_config import ManagedConfigError


SYSTEM_DIRECTORIES = ('/usr/local/bin', '/usr/bin', '/bin',
                      '/usr/local/sbin', '/usr/sbin', '/sbin')


def normalize_path(value: str) -> str:
    if any(character in value for character in '\0\r\n'):
        raise ManagedConfigError('PATH에 NUL 또는 줄바꿈을 사용할 수 없습니다.')
    directories = {}
    for item in value.split(os.pathsep):
        path = Path(item)
        if path.is_absolute() and path.is_dir():
            directories.setdefault(str(path), None)
    return os.pathsep.join(directories)


def save_execution_path(source: Path) -> None:
    if sys.platform != 'linux':
        return
    value = normalize_path(os.environ.get('PATH', ''))
    if (not value or not shutil.which('git', path=value) or not shutil.which('gh', path=value)
            or not any(shutil.which(provider, path=value) for provider in ('codex', 'claude'))):
        raise ManagedConfigError('Git·gh와 Codex 또는 Claude를 찾을 수 있는 터미널에서 setup을 실행하세요.')
    destination = source / 'config/execution-path.txt'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                prefix='.execution-path-', dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(value + '\n')
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_execution_path(source: Path) -> None:
    if sys.platform != 'linux':
        return
    cache = source / 'config/execution-path.txt'
    try:
        try:
            stored = cache.read_bytes().decode('utf-8').removesuffix('\n')
        except FileNotFoundError:
            if cache.is_symlink():
                raise ManagedConfigError('PATH 캐시의 연결 대상이 없습니다.')
            stored = None
        if stored is not None:
            if not stored or not any(Path(item).is_absolute() for item in stored.split(os.pathsep)):
                raise ManagedConfigError('PATH 캐시가 비어 있거나 절대 경로가 없습니다.')
            normalize_path(stored)
    except (OSError, UnicodeError, ManagedConfigError) as error:
        raise ManagedConfigError(f'PATH 캐시를 읽을 수 없습니다: {cache}. 정상 터미널에서 setup을 다시 실행하세요.') from error
    os.environ['PATH'] = normalize_path(os.pathsep.join((
        stored or '', os.environ.get('PATH', ''), *SYSTEM_DIRECTORIES,
    )))
