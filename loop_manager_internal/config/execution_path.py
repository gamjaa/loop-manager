"""Linux 설치별 PATH 캐시와 bootstrap 실행 환경."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from loop_manager_internal.config.managed_config import ManagedConfigError


SYSTEM_DIRECTORIES = ('/usr/local/bin', '/usr/bin', '/bin',
                      '/usr/local/sbin', '/usr/sbin', '/sbin')
_COMMAND_NOT_FOUND = (
    re.compile(
        r"(?im)^[ \t]*(?:(?:/[^\s:]+/)?(?:sh|bash|dash|zsh)"
        r"(?::[ \t]+(?:line[ \t]+\d+|\d+))?:[ \t]+)?"
        r"[A-Za-z0-9_.+-]+:[ \t]+(?:command not found|not found)[ \t]*$"
    ),
    re.compile(
        r"(?im)^[ \t]*(?:/usr/bin/)?env:[ \t]*[\"'‘]?"
        r"[A-Za-z0-9_.+-]+[\"'’]?:[ \t]*No such file or directory[ \t]*$"
    ),
    re.compile(r"(?im)^[ \t]*(?:Error:[ \t]*)?spawn(?:Sync)?[ \t]+[\"']?[A-Za-z0-9_./+-]+[\"']?[ \t]+ENOENT\b"),
    re.compile(
        r"(?im)^[ \t]*(?:FileNotFoundError: )?\[Errno 2\] No such file or directory: "
        r"[\"'](?:[^\"']*/)?[A-Za-z0-9_.+-]+[\"']"
    ),
)


def path_recovery_hint() -> str:
    if sys.platform != 'linux':
        return ''
    return (
        '같은 실행 계정의 정상 터미널에서 설치 경로를 확인한 뒤 '
        'Loop Manager source checkout에서 `bash scripts/setup.sh`를 실행해 PATH를 갱신하세요.'
    )


def missing_executable(error: OSError, executable: str | None = None) -> bool:
    if (sys.platform != 'linux' or not isinstance(error, FileNotFoundError)
            or error.errno != errno.ENOENT or not error.filename):
        return False
    name = Path(os.fsdecode(error.filename)).name
    return executable is None or name == executable


def command_lookup_failed(output: str) -> bool:
    return any(pattern.search(output) for pattern in _COMMAND_NOT_FOUND)


def with_path_recovery_hint(output: str) -> str:
    hint = path_recovery_hint()
    if not hint or hint in output:
        return output
    return f'{output.rstrip()}\n{hint}'


def append_path_recovery_hint(output: str) -> str:
    if not command_lookup_failed(output):
        return output
    return with_path_recovery_hint(output)


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
        raise ManagedConfigError(
            f'PATH 캐시를 읽을 수 없습니다: {cache}. {path_recovery_hint()}'
        ) from error
    os.environ['PATH'] = normalize_path(os.pathsep.join((
        stored or '', os.environ.get('PATH', ''), *SYSTEM_DIRECTORIES,
    )))
