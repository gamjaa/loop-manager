"""상태 루트에서 선택한 공급자 홈과 공유 인증 파일을 준비한다."""
from __future__ import annotations

import os
from pathlib import Path
import shlex


PROVIDERS = {
    'codex': ('CODEX_HOME', '.codex', 'auth.json', 'codex login', 'codex login status'),
    'claude': ('CLAUDE_CONFIG_DIR', '.claude', '.credentials.json', 'claude auth login', 'claude auth status'),
}


def login_instructions(provider: str, home: Path) -> str:
    variable, _, _, login, status = PROVIDERS[provider]
    if os.name == 'nt':
        prefix = f"$env:{variable}='{str(home).replace(chr(39), chr(39) * 2)}'; "
    else:
        prefix = f'env {variable}={shlex.quote(str(home))} '
    return f'{prefix}{login} / 상태 확인: {prefix}{status}'


def prepare_authentication(provider: str, environment: dict[str, str]) -> None:
    from .agent_codex_sandbox import _owned_directory

    variable, folder, filename, _, _ = PROVIDERS[provider]
    home = Path(environment[variable])
    user_home = Path(environment.get('USERPROFILE' if os.name == 'nt' else 'HOME', str(Path.home())))
    source, auth = user_home / folder / filename, home / filename
    try:
        home.parent.mkdir(parents=True, exist_ok=True)
        _owned_directory(home)
        if not auth.exists() and not auth.is_symlink():
            if source.exists() or source.is_symlink():
                if not source.is_file():
                    raise OSError('기본 인증 파일을 읽을 수 없습니다')
                with source.open('rb'):
                    pass
                try:
                    auth.symlink_to(source.absolute())
                except FileExistsError:
                    pass  # 먼저 생성된 인증 파일은 덮어쓰지 않는다.
        if auth.exists() or auth.is_symlink():
            if not auth.is_file():
                raise OSError('기존 인증 파일 또는 링크를 읽을 수 없습니다')
            with auth.open('rb'):
                pass
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f'{provider} 전용 인증 경로 준비 실패: {home} ({error}). '
            f'{login_instructions(provider, home)}'
        ) from error


def authentication_links(provider: str, environment: dict[str, str]) -> tuple[tuple[Path, Path], ...]:
    variable, _, filename, _, _ = PROVIDERS[provider]
    auth = Path(environment[variable]) / filename
    if auth.is_symlink():
        auth.resolve(strict=True)
    links = []
    while auth.is_symlink():
        target = auth.readlink()
        if any(path == auth for path, _ in links):
            raise RuntimeError(f'{provider} 인증 링크가 순환합니다: {auth}')
        links.append((auth, target))
        auth = target if target.is_absolute() else auth.parent / target
    if links and not auth.is_file():
        raise RuntimeError(f'{provider} 인증 링크의 원본을 읽을 수 없습니다: {auth}')
    return tuple(links)


def assert_authentication_links(provider: str, environment: dict[str, str],
                                links: tuple[tuple[Path, Path], ...]) -> None:
    variable = PROVIDERS[provider][0]
    try:
        if any(not path.is_symlink() or path.readlink() != target for path, target in links):
            raise OSError('CLI 호출 중 인증 링크가 변경됐습니다')
        authentication_links(provider, environment)
    except (OSError, RuntimeError) as error:
        home = Path(environment[variable])
        raise RuntimeError(f'{provider} 인증 공유 확인 실패: {home}. '
                           f'{login_instructions(provider, home)}') from error
