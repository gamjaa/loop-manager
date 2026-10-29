"""Read-only Claude startup checks, separate from native enforcement observations."""
from __future__ import annotations

from dataclasses import dataclass, field
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
from .agent_write_isolation import IsolationError, IsolationEvidence, WorktreeBoundary, _policy_digest
from loop_manager_internal.config import execution_path
from loop_manager_internal.execution.validation_feedback import redact_secrets


CLI_GUARDS = ('--permission-prompts', 'none', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}')
POLICY_DIAGNOSTIC = re.compile(
    r'settings? (?:error|warning):|(?:sandbox|auto mode).{0,60}(?:unavailable|disabled|fail(?:ed|ure))', re.I)
MANAGED_KEYS = frozenset({
    'permissions', 'sandbox', 'autoMode', 'disableAutoMode', 'disableAllHooks',
    'fastMode', 'model', 'effortLevel', 'maxEffortLevel', 'managedSourcesBehavior',
    'wslInheritsWindowsSettings', 'env', 'hooks', 'statusLine', 'subagentStatusLine',
    'fileSuggestion', 'enabledPlugins', 'managedMcpServers', 'mcpServers', 'policyHelper',
    'apiKeyHelper', 'otelHeadersHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh',
})


@dataclass(frozen=True)
class ClaudeLaunch:
    arguments: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)
    runtime: Path
    attempt: Path
    specification: str
    cli_version: str
    configuration_digest: str
    reason: str
    probe_arguments: tuple[str, ...] = ()
    bash_temporary: Path | None = None


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
        if set(source) - MANAGED_KEYS:
            raise IsolationError('Claude managed source key의 유효 우선순위를 확인할 수 없습니다.')
        if 'model' in source and (not isinstance(source['model'], str) or not source['model']):
            raise IsolationError('Claude managed model 형식이 올바르지 않습니다.')
        for key in ('fastMode', 'disableAllHooks'):
            if key in source and type(source[key]) is not bool:
                raise IsolationError(f'Claude managed {key} 형식이 올바르지 않습니다.')
        if 'managedSourcesBehavior' in source and source['managedSourcesBehavior'] not in ('first-wins', 'merge'):
            raise IsolationError('Claude managedSourcesBehavior를 해석할 수 없습니다.')
        for key in ('permissions', 'sandbox', 'autoMode', 'env'):
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
    # Control-only documents select composition even though they supply no policy.
    controls = next(source for source in (remote, mdm, files)
                    if 'managedSourcesBehavior' in source or source in sources)
    behavior = controls.get('managedSourcesBehavior', 'first-wins')
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


def _mdm_paths() -> tuple[Path, Path]:
    import pwd  # Native Windows is rejected before any POSIX settings lookup.
    directory = Path('/Library/Managed Preferences')
    name = 'com.anthropic.claudecode.plist'
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except KeyError as error:
        raise IsolationError('Claude MDM 사용자의 OS identity를 확인할 수 없습니다.') from error
    return directory / name, directory / username / name


def _read(path: Path, observations: list[dict], *, parse: bool = True, plist: bool = False) -> dict:
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
        value = plistlib.loads(raw) if plist else json.loads(raw)
        if not isinstance(value, dict) or any(key in value for key in ('settings', 'managedSettings')):
            raise ValueError('expected bare settings object')
        return value
    except (ValueError, UnicodeError, RecursionError, plistlib.InvalidFileException) as error:
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
        for path in _mdm_paths():
            candidate = _read(path, observations, plist=True)
            if mdm and candidate and mdm != candidate:
                raise IsolationError('Claude macOS MDM source의 유효 우선순위를 확인할 수 없습니다.')
            mdm = candidate or mdm
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
    if POLICY_DIAGNOSTIC.search(result.stderr or ''):
        raise IsolationError('Claude 시작 확인 stderr에서 정책 오류·무시를 관측했습니다.')
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
    from .provider_home import prepare_authentication
    prepare_authentication('claude', environment)
    _owned_directory(base)
    runtime = base / sha256(('claude\0' + boundary.repository_identity + '\0'
                            + str(boundary.worktree) + '\0' + role).encode()).hexdigest()
    _owned_directory(runtime)
    attempt = Path(tempfile.mkdtemp(prefix='call-', dir=runtime))
    # Keep Bash below native temp/socket path limits without granting the shared
    # claude-UID fallback. Receipts and targeted records stay in the state runtime.
    short_base = Path('/tmp').resolve()
    if any(short_base == path or short_base.is_relative_to(path)
           for path in (boundary.source, boundary.worktree, *boundary.other_worktrees,
                        boundary.common_dir, boundary.git_dir, home)):
        raise IsolationError('Claude 짧은 임시 경로와 보호 경로가 겹칩니다.')
    bash_temporary = Path(tempfile.mkdtemp(prefix='lm-', dir=short_base))
    child = dict(environment, TMPDIR=str(bash_temporary), TMP=str(bash_temporary), TEMP=str(bash_temporary),
                 CLAUDE_CODE_TMPDIR=str(bash_temporary), CLAUDE_CODE_AUTO_CONNECT_IDE='0')
    if role == 'worker' and not read_only:
        child['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'] = str(attempt)
        child['LOOP_MANAGER_TARGETED_TEST_RUNNER'] = str(Path(__file__).resolve().parents[1] / 'verification/targeted.py')
    effective, _, _ = configuration(child, config_path)
    arguments = policy_arguments(boundary, attempt, read_only, fast_mode=fast_mode, managed_settings=effective,
                                 bash_temporary=bash_temporary) + CLI_GUARDS
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
                {'claude.ai', 'oauth_token', 'api_key', 'third_party'}
                or not isinstance(authentication.get('configDirectory'), str)
                or Path(authentication['configDirectory']).resolve() != home):
            raise ValueError('authentication home or method unavailable')
    except (ValueError, TypeError, RecursionError) as error:
        raise IsolationError('Claude 인증 상태·선택 공통 홈을 확인할 수 없습니다.') from error
    # doctor is a documented read-only settings diagnostic. Neither its exit
    # status nor the requested settings are an OS write-probe success.
    doctor = _command(['claude', *arguments, 'doctor'], child, boundary.worktree)
    if POLICY_DIAGNOSTIC.search(doctor):
        raise IsolationError('Claude 시작 설정 진단에서 정책 오류·무시를 관측했습니다.')
    effective, digest, observations = configuration(child, config_path)
    arguments = policy_arguments(boundary, attempt, read_only, fast_mode=fast_mode, managed_settings=effective,
                                 bash_temporary=bash_temporary) + CLI_GUARDS
    specification = json.dumps({'worktree': str(boundary.worktree), 'source': str(boundary.source),
        'repository': boundary.repository_identity, 'read_only': read_only, 'role': role,
        'git_directory': str(boundary.git_dir), 'common_directory': str(boundary.common_dir),
        'temporary': str(attempt), 'bash_temporary': str(bash_temporary), 'settings_sources': observations}, sort_keys=True)
    reason = json.dumps({'basis': 'documented-native-configuration', 'runtime_enforcement': 'not_observed',
        'authentication_method': authentication['authMethod'], 'authentication_home': str(home),
        'auto_mode_support': 'requires_invocation_init',
        'requested_policy_digest': sha256(json.dumps(arguments).encode()).hexdigest(),
        'settings_sources': observations,
        'effective_managed_digest': sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()}, sort_keys=True)
    return ClaudeLaunch(arguments, child, runtime, attempt, specification, version, digest, reason, bash_temporary=bash_temporary)


def assert_current(launch: ClaudeLaunch, boundary: WorktreeBoundary, role: str,
                   read_only: bool, config_path: Path | None, *, check_cli: bool = True) -> None:
    """Recheck one prepared call without comparing mutable Git state."""
    specification = json.loads(launch.specification)
    if any(specification.get(key) != value for key, value in {
            'worktree': str(boundary.worktree), 'source': str(boundary.source),
            'repository': boundary.repository_identity, 'role': role,
            'git_directory': str(boundary.git_dir), 'common_directory': str(boundary.common_dir),
            'read_only': read_only, 'temporary': str(launch.attempt),
            'bash_temporary': str(launch.bash_temporary)}.items()):
        raise IsolationError('Claude 시작 확인과 현재 호출 identity가 다릅니다.')
    if launch.bash_temporary is None:
        raise IsolationError('Claude Bash 소유 임시 경로가 없습니다.')
    for path in (launch.runtime, launch.attempt, launch.bash_temporary):
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
    if check_cli:
        version = _command(['claude', '--version'], launch.environment, boundary.worktree)
        if version != launch.cli_version:
            raise IsolationError('Claude 시작 확인 뒤 CLI 버전이 변경됐습니다.')


def isolate(role, worktree, state_dir, read_only, observe, invocation_digest, *,
            config_path=None, expected_repository=None, session_id=None,
            environment_variables=None, prepared_launch=None):
    """Bind configuration preparation to one invocation without claiming enforcement."""
    boundary = None
    launch = None
    status, reason, scope = 'rejected', '', ''
    try:
        from . import agent_write_isolation
        boundary = agent_write_isolation.inspect_worktree(worktree, state_dir, read_only=read_only)
        if expected_repository is None or Path(expected_repository).resolve() != boundary.source:
            raise IsolationError('claude 선택 worktree와 등록 repository identity가 다릅니다.')
        if platform.system() not in {'Linux', 'Darwin'}:
            raise IsolationError(f'claude {platform.system()}의 native sandbox를 지원하지 않습니다.')
        if environment_variables is None:
            raise IsolationError('claude 호출의 필터된 실행 환경이 없습니다.')
        if invocation_digest is None or not (scope := invocation_digest()):
            raise IsolationError('claude 호출 identity 근거가 없습니다.')
        if prepared_launch is not None:
            if not isinstance(prepared_launch, ClaudeLaunch):
                raise IsolationError('claude 호출의 사전 준비 자료 형식이 다릅니다.')
            launch = prepared_launch
            if (launch.runtime / (launch.attempt.name + '.process.json')).exists():
                raise IsolationError('claude 이전 호출의 준비·실행 자료를 재사용할 수 없습니다.')
            if any(environment_variables.get(key) != launch.environment.get(key)
                   for key in ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'PATH')):
                raise IsolationError('claude 준비 환경과 실제 호출 환경이 다릅니다.')
            assert_current(launch, boundary, role, read_only, config_path)
        else:
            from loop_manager_internal.config.managed_config import provider_fast_mode
            launch = prepare(boundary, state_dir, role, read_only, environment_variables, config_path,
                fast_mode=provider_fast_mode(config_path, 'claude') if config_path is not None else False)
        status, reason = 'prepared', launch.reason
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        reason = redact_secrets(str(error)) if isinstance(error, RuntimeError) else 'claude 시작 설정 확인 실패'
    environment = f'{platform.system()}:{platform.release()}:{platform.machine()}'
    version = launch.cli_version if launch else 'unknown'
    canonical = str(boundary.worktree) if boundary else str(worktree)
    repository = boundary.repository_identity if boundary else ''
    configuration_digest = launch.configuration_digest if launch else ''
    evidence = IsolationEvidence('claude', version, environment, role, canonical, repository,
        _policy_digest('claude', version, environment, role, canonical, repository,
                       configuration_digest, launch.arguments if launch else (), launch, session_id),
        status, reason, scope, configuration_digest)
    if observe is not None:
        observe(evidence)
    if status != 'prepared':
        raise IsolationError(reason)
    return launch


def check_event(event: dict, launch: ClaudeLaunch, model: str, initialized: bool,
                *, session_id: str | None = None) -> bool:
    """Inspect native startup/error events, not a live managed-settings watcher."""
    kind = event.get('type')
    if kind == 'system' and event.get('subtype') == 'init':
        specification = json.loads(launch.specification)
        tools = event.get('tools')
        allowed = set(launch.arguments[launch.arguments.index('--tools') + 1].split(','))
        actual_model = event.get('model')
        if (event.get('permissionMode') != 'auto'
                or event.get('cwd') != specification['worktree']
                or event.get('claude_code_version') != launch.cli_version.split()[0]
                or not isinstance(actual_model, str)
                or re.sub(r'-\d{8}$', '', actual_model) != re.sub(r'-\d{8}$', '', model)
                or not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools)
                or not {'Bash', 'Read'} <= set(tools)
                or set(tools) - allowed - {'EndConversation'}
                or event.get('mcp_servers') != [] or event.get('plugins') != []
                or (session_id is not None and event.get('session_id') != session_id)):
            raise IsolationError('Claude auto mode·모델·CLI·workspace·도구 시작 관측이 요청과 다릅니다.')
        return True
    if (kind == 'system' and 'permissionMode' in event and event['permissionMode'] != 'auto'):
        raise IsolationError('Claude auto mode가 실제 호출에서 비활성화됐습니다.')
    if (kind == 'system' and event.get('subtype') == 'permission_denied') or event.get('permission_denials'):
        raise IsolationError('Claude 실제 도구 권한 거부를 관측했습니다.')
    message = event.get('message')
    if isinstance(message, dict) and isinstance(message.get('content'), list):
        for content in message['content']:
            if (isinstance(content, dict) and content.get('type') == 'tool_result'
                    and content.get('is_error') is True):
                diagnostic = str(content.get('content', ''))
                if re.search(r'permission denied|EACCES|EPERM|sandbox|classifier|authentication', diagnostic, re.I):
                    raise IsolationError('Claude 실제 권한·인증·보안 도구 오류를 관측했습니다.')
    # Resume-not-found and other native errors retain the existing recovery
    # classification. A successful result cannot bypass initialization.
    if not initialized and kind in {'assistant', 'user', 'result'} and not event.get('is_error'):
        raise IsolationError('Claude auto mode 시작 관측보다 먼저 실행 결과를 받았습니다.')
    return initialized
