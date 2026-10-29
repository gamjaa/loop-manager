"""Read-only Claude startup checks, separate from native enforcement observations."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import subprocess
import tempfile

from .agent_claude_policy import policy_arguments
from .agent_codex_sandbox import _owned_directory
from .agent_write_isolation import IsolationError, WorktreeBoundary
from loop_manager_internal.config import execution_path
from loop_manager_internal.execution.validation_feedback import redact_secrets


CLI_GUARDS = ('--permission-prompts', 'none', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}')


@dataclass(frozen=True)
class ClaudeLaunch:
    arguments: tuple[str, ...]
    environment: dict[str, str]
    runtime: Path
    attempt: Path
    specification: str
    cli_version: str
    configuration_digest: str
    reason: str
    probe_arguments: tuple[str, ...] = ()


def _merge(lower: dict, higher: dict) -> dict:
    result = dict(lower)
    for key, value in higher.items():
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _merge(old, value)
        elif isinstance(old, list) and isinstance(value, list) and key != 'fallbackModel':
            # ponytail: O(n²) dedup for small native settings lists; index
            # canonical JSON values if large policy lists become a real input.
            result[key] = old + [item for item in value if item not in old]
        else:
            result[key] = value
    return result


def resolve_managed(remote: dict, mdm: dict, files: dict) -> dict:
    """Resolve the native managed ranking, preserving cross-source restrictions."""
    for source in (remote, mdm, files):
        if not isinstance(source, dict):
            raise IsolationError('Claude managed source가 JSON object가 아닙니다.')
        for key in ('permissions', 'sandbox'):
            if key in source and not isinstance(source[key], dict):
                raise IsolationError(f'Claude managed {key} source 형식이 올바르지 않습니다.')
        for key in ('filesystem', 'network'):
            if key in source.get('sandbox', {}) and not isinstance(source['sandbox'][key], dict):
                raise IsolationError(f'Claude managed {key} source 형식이 올바르지 않습니다.')
    sources = [source for source in (remote, mdm, files)
               if any(value is not None and key not in {'managedSourcesBehavior', 'wslInheritsWindowsSettings'}
                      for key, value in source.items())]
    if not sources:
        return {}
    selected = sources[0]
    behavior = selected.get('managedSourcesBehavior', 'first-wins')
    if behavior not in {'first-wins', 'merge'}:
        raise IsolationError('Claude managedSourcesBehavior를 해석할 수 없습니다.')
    effective = dict(selected)
    if behavior == 'merge':
        for source in sources[1:]:
            effective = _merge(source, effective)
        # Native defaultMode is read only from the highest-ranked source.
        if 'permissions' in effective:
            effective['permissions'] = dict(effective['permissions'])
            effective['permissions'].pop('defaultMode', None)
            if 'defaultMode' in selected.get('permissions', {}):
                effective['permissions']['defaultMode'] = selected['permissions']['defaultMode']
    # These inputs are read from lower admin sources even with first-wins.
    # Refuse unsupported native binary overrides and disabled filesystem
    # isolation rather than accidentally treating a shadowed source as inert.
    for source in sources:
        sandbox = source.get('sandbox', {})
        if not isinstance(sandbox, dict):
            raise IsolationError('Claude managed sandbox source 형식이 올바르지 않습니다.')
        filesystem = sandbox.get('filesystem', {})
        if not isinstance(filesystem, dict):
            raise IsolationError('Claude managed filesystem source 형식이 올바르지 않습니다.')
        if (('disabled' in filesystem and filesystem['disabled'] is not False)
                or any(sandbox.get(key) for key in ('bwrapPath', 'socatPath', 'ripgrep'))):
            raise IsolationError('Claude cross-source sandbox 정책이 쓰기 제한과 충돌합니다.')
        if source.get('env'):
            raise IsolationError('Claude cross-source env 정책의 유효 경계를 확인할 수 없습니다.')
        if filesystem.get('allowManagedReadPathsOnly') or sandbox.get('network', {}).get('allowManagedDomainsOnly'):
            raise IsolationError('Claude cross-source sandbox allowlist lock 해석이 지원되지 않습니다.')
    return effective


def _system_directory() -> Path:
    return Path('/Library/Application Support/ClaudeCode' if platform.system() == 'Darwin'
                else '/etc/claude-code')


def _read(path: Path, observations: list[dict], *, parse: bool = True) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                raise IsolationError('Claude 설정 source가 제한된 일반 파일이 아닙니다.')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(raw) > 2 * 1024 * 1024:
            raise IsolationError('Claude 설정 source가 너무 큽니다.')
    except FileNotFoundError:
        if path.is_symlink():
            raise IsolationError('Claude 설정 source 링크가 끊어졌습니다.')
        observations.append({'path': str(path), 'resolved': str(path.resolve()), 'absent': True})
        return {}
    except OSError as error:
        raise IsolationError(f'Claude 설정 source를 읽을 수 없습니다: {path}') from error
    observations.append({'path': str(path), 'resolved': str(path.resolve()), 'sha256': sha256(raw).hexdigest()})
    if not parse:
        return {}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or any(key in value for key in ('settings', 'managedSettings')):
            raise ValueError('expected bare settings object')
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise IsolationError(f'Claude 설정 source 형식을 해석할 수 없습니다: {path}') from error


def configuration(environment: dict[str, str], config_path: Path | None) -> tuple[dict, str, list[dict]]:
    """Observe actual managed source identities, without persisting their values."""
    observations: list[dict] = [{'environment': {key: value for key, value in environment.items()
        if key in {'HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'TMPDIR', 'TMP', 'TEMP',
                   'CLAUDE_CODE_TMPDIR', 'PATH'}}}]
    system = _system_directory()
    files = _read(system / 'managed-settings.json', observations)
    dropins = system / 'managed-settings.d'
    try:
        with os.scandir(dropins) as entries:
            names = sorted(entry.name for entry in entries
                           if not entry.name.startswith('.') and entry.name.endswith('.json'))
    except FileNotFoundError:
        if dropins.is_symlink():
            raise IsolationError('Claude managed drop-in 링크가 끊어졌습니다.')
        names = []
    except OSError as error:
        raise IsolationError('Claude managed drop-in 목록을 읽을 수 없습니다.') from error
    observations.append({'path': str(dropins), 'resolved': str(dropins.resolve()), 'names': names})
    for name in names:
        files = _merge(files, _read(dropins / name, observations))
    mdm = {}
    if platform.system() == 'Darwin':
        try:
            result = subprocess.run(['defaults', 'export', 'com.anthropic.claudecode', '-'],
                capture_output=True, timeout=15, check=False, env=environment)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise IsolationError('Claude MDM 설정 domain 조회 실패') from error
        if result.returncode:
            if b'does not exist' not in result.stderr:
                raise IsolationError('Claude MDM 설정 domain을 확인할 수 없습니다.')
        else:
            try:
                mdm = plistlib.loads(result.stdout)
                if not isinstance(mdm, dict):
                    raise ValueError('expected plist dictionary')
            except (ValueError, plistlib.InvalidFileException) as error:
                raise IsolationError('Claude MDM 설정을 해석할 수 없습니다.') from error
        observations.append({'source': 'com.anthropic.claudecode',
                             'sha256': sha256(result.stdout).hexdigest()})
    if 'microsoft' in platform.release().lower():
        raise IsolationError('Claude WSL의 Windows managed 정책 상속을 확인할 수 없습니다.')
    remote = _read(Path(environment['CLAUDE_CONFIG_DIR']) / 'remote-settings.json', observations)
    if config_path is not None:
        _read(config_path, observations, parse=False)
    effective = resolve_managed(remote, mdm, files)
    fingerprint = sha256(json.dumps(observations, sort_keys=True).encode()).hexdigest()
    return effective, fingerprint, observations


def _command(arguments: list[str], environment: dict[str, str], worktree: Path) -> str:
    try:
        result = subprocess.run(arguments, env=environment, cwd=worktree, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        if execution_path.missing_executable(error, arguments[0]):
            raise IsolationError(execution_path.with_path_recovery_hint('Claude CLI를 찾을 수 없습니다.')) from error
        raise IsolationError('Claude 시작 확인 도구 실행 실패') from error
    if result.returncode:
        diagnostic = redact_secrets('\n'.join(filter(None, (result.stdout, result.stderr))))[-6000:]
        raise IsolationError(f'Claude 시작 확인 실패 (exit {result.returncode}): {diagnostic}')
    return result.stdout.strip()


def prepare(boundary: WorktreeBoundary, state_dir: Path, role: str, read_only: bool,
            environment: dict[str, str], config_path: Path | None, *, fast_mode: bool = False) -> ClaudeLaunch:
    if platform.system() not in {'Linux', 'Darwin'}:
        raise IsolationError('Claude native Windows·미지원 OS에는 내장 sandbox가 없습니다.')
    if role not in {'planner', 'planner_check', 'maintenance_planner', 'worker', 'reviewer'}:
        raise IsolationError('Claude 관리 호출 역할을 확인할 수 없습니다.')
    if role in {'reviewer', 'planner_check'} and not read_only:
        raise IsolationError('Claude 읽기 전용 역할에 쓰기 권한을 요청했습니다.')
    if not environment.get('CLAUDE_CONFIG_DIR'):
        raise IsolationError('Claude 공통 인증 홈이 선택되지 않았습니다.')
    home = Path(environment['CLAUDE_CONFIG_DIR'])
    if not home.is_absolute() or home != home.resolve():
        raise IsolationError('Claude 공통 인증 홈이 canonical 경로가 아닙니다.')
    if home == boundary.worktree or home.is_relative_to(boundary.worktree) or boundary.worktree.is_relative_to(home):
        raise IsolationError('Claude 공통 인증 홈과 workspace가 겹칩니다.')
    base = state_dir.resolve() / 'agent-runtime'
    for protected in (boundary.source, boundary.worktree, *boundary.other_worktrees,
                      boundary.common_dir, boundary.git_dir, home):
        if base == protected or base.is_relative_to(protected) or protected.is_relative_to(base):
            raise IsolationError('Claude runtime과 보호 경로가 겹칩니다.')
    _owned_directory(base)
    runtime = base / sha256(('claude\0' + boundary.repository_identity + '\0'
                            + str(boundary.worktree) + '\0' + role).encode()).hexdigest()
    _owned_directory(runtime)
    attempt = Path(tempfile.mkdtemp(prefix='call-', dir=runtime))
    child = dict(environment, TMPDIR=str(attempt), TMP=str(attempt), TEMP=str(attempt),
                 CLAUDE_CODE_TMPDIR=str(attempt), CLAUDE_CODE_AUTO_CONNECT_IDE='0')
    if role == 'worker' and not read_only:
        child['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'] = str(attempt)
        child['LOOP_MANAGER_TARGETED_TEST_RUNNER'] = str(Path(__file__).resolve().parents[1] / 'verification/targeted.py')
    effective, _, _ = configuration(child, config_path)
    arguments = policy_arguments(boundary, attempt, read_only, fast_mode=fast_mode, managed_settings=effective) + CLI_GUARDS
    version = _command(['claude', '--version'], child, boundary.worktree)
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?', version)
    # Includes restricted, excluded-source filesystem merging and the native
    # 2.1.307 fix that stops sandbox cleanup deleting nonempty objects/refs.
    if not match or tuple(map(int, match.groups())) < (2, 1, 307):
        raise IsolationError('Claude CLI가 지원되는 native 정책 버전(v2.1.307 이상)이 아닙니다.')
    help_text = _command(['claude', '--help'], child, boundary.worktree)
    if any(flag not in help_text for flag in ('--restricted', '--setting-sources', '--settings',
            '--permission-mode', '--permission-prompts', '--tools', '--disallowedTools', '--strict-mcp-config')):
        raise IsolationError('Claude CLI의 필수 native 정책 옵션을 확인할 수 없습니다.')
    try:
        authentication = json.loads(_command(['claude', *arguments, 'auth', 'status'], child, boundary.worktree))
        if (not isinstance(authentication, dict) or authentication.get('authMethod') not in
                {'claude.ai', 'oauth_token', 'api_key', 'api_key_helper', 'third_party'}
                or not isinstance(authentication.get('configDirectory'), str)
                or Path(authentication['configDirectory']).resolve() != home):
            raise ValueError('authentication home or method unavailable')
    except (ValueError, TypeError, RecursionError) as error:
        raise IsolationError('Claude 인증 상태·선택 공통 홈을 확인할 수 없습니다.') from error
    # doctor is a documented read-only settings diagnostic. Neither its exit
    # status nor the requested settings are an OS write-probe success.
    doctor = _command(['claude', *arguments, 'doctor'], child, boundary.worktree)
    if re.search(r'settings? (?:error|warning):|(?:sandbox|auto mode).{0,60}(?:unavailable|disabled|fail(?:ed|ure))', doctor, re.I):
        raise IsolationError('Claude 시작 설정 진단에서 정책 오류·무시를 관측했습니다.')
    effective, digest, observations = configuration(child, config_path)
    arguments = policy_arguments(boundary, attempt, read_only, fast_mode=fast_mode, managed_settings=effective) + CLI_GUARDS
    specification = json.dumps({'worktree': str(boundary.worktree), 'source': str(boundary.source),
        'repository': boundary.repository_identity, 'read_only': read_only, 'role': role,
        'temporary': str(attempt), 'settings_sources': observations}, sort_keys=True)
    reason = json.dumps({'basis': 'documented-native-configuration', 'runtime_enforcement': 'not_observed',
        'authentication_method': authentication['authMethod'], 'authentication_home': str(home),
        'auto_mode_support': 'requires_invocation_init',
        'requested_policy_digest': sha256(json.dumps(arguments).encode()).hexdigest(),
        'effective_managed_digest': sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()}, sort_keys=True)
    return ClaudeLaunch(arguments, child, runtime, attempt, specification, version, digest, reason)


def assert_current(launch: ClaudeLaunch, boundary: WorktreeBoundary, role: str,
                   read_only: bool, config_path: Path | None) -> None:
    """Recheck one prepared call without comparing mutable Git state."""
    specification = json.loads(launch.specification)
    if any(specification.get(key) != value for key, value in {
            'worktree': str(boundary.worktree), 'source': str(boundary.source),
            'repository': boundary.repository_identity, 'role': role,
            'read_only': read_only, 'temporary': str(launch.attempt)}.items()):
        raise IsolationError('Claude 시작 확인과 현재 호출 identity가 다릅니다.')
    for path in (launch.runtime, launch.attempt):
        try:
            info = path.lstat()
        except OSError as error:
            raise IsolationError('Claude 소유 runtime 경로를 다시 확인할 수 없습니다.') from error
        if (not stat.S_ISDIR(info.st_mode) or path != path.resolve()
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise IsolationError('Claude 소유 runtime 경로·권한이 변경됐습니다.')
    if launch.attempt.parent != launch.runtime:
        raise IsolationError('Claude 호출별 임시 경로가 runtime에 속하지 않습니다.')
    _, digest, _ = configuration(launch.environment, config_path)
    if digest != launch.configuration_digest:
        raise IsolationError('Claude 시작 확인 뒤 유효 설정 source가 변경됐습니다.')
    version = _command(['claude', '--version'], launch.environment, boundary.worktree)
    if version != launch.cli_version:
        raise IsolationError('Claude 시작 확인 뒤 CLI 버전이 변경됐습니다.')
