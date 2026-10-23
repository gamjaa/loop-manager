"""Compare immutable runtime inputs, excluding only recognizable prose blobs."""
from pathlib import PurePosixPath
import ast


def prose(path: str, mode: str, content: bytes) -> bool:
    name = PurePosixPath(path)
    if path.startswith('docs/AGENTS.') and name.suffix.lower() == '.md':
        return False
    documented = (
        name.suffix.lower() in {'.md', '.rst', '.txt'}
        and (path.startswith(('docs/', '.agents/skills/'))
             or name.name.upper().startswith(('README', 'AGENTS', 'LICENSE', 'CONTRIBUTING')))
    )
    if not documented or mode != '100644' or b'\0' in content or content.startswith(b'#!'):
        return False
    try:
        decoded = content.decode('utf-8')
    except UnicodeDecodeError:
        return False
    try:
        executable = ast.parse(decoded)
    except SyntaxError:
        return True
    if executable.body:
        return False
    return True


def changed(git, repository, running: str, remote: str) -> bool:
    """Unknown paths/kinds and executable documentation remain runtime inputs."""
    raw = git(repository, 'diff', '--raw', '--no-abbrev', '--no-renames', '-z', running, remote).decode('utf-8')
    fields = raw.split('\0')
    if fields.pop() != '' or len(fields) % 2:
        raise RuntimeError('runtime 입력 diff를 읽을 수 없습니다.')
    for header, path in zip(fields[::2], fields[1::2]):
        old_mode, new_mode, old_oid, new_oid, status = header.removeprefix(':').split()
        if not header.startswith(':') or status not in {'A', 'D', 'M', 'T'} or not path:
            raise RuntimeError('runtime 입력 diff 형식이 유효하지 않습니다.')
        if old_mode != new_mode and '000000' not in {old_mode, new_mode}:
            return True
        for mode, oid in ((old_mode, old_oid), (new_mode, new_oid)):
            if mode == '000000':
                continue
            if mode != '100644':
                return True
            content = git(repository, 'cat-file', 'blob', oid)
            if not prose(path, mode, content):
                return True
    return False
