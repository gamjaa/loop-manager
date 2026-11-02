"""Compile a Claude native tool policy; this is not proof of CLI enforcement.

The caller must discover the effective managed settings, check CLI/account/model
support, own the temporary directory, and bind/recheck the result per invocation.
All consumed sources are validated before native settings merging.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import tempfile

from .agent_write_isolation import IsolationError, WorktreeBoundary


def _canonical(path: Path) -> Path:
    # Sandbox write lists treat glob characters differently across platforms.
    # Refuse ambiguous roots rather than grant a broader pattern on macOS.
    if (not path.is_absolute() or path != path.resolve()
            or any(character in str(path) for character in '*?[]\\\n\r\x00')):
        raise IsolationError('Claude 쓰기 정책에 모호한 경로를 사용할 수 없습니다.')
    return path


def _object(settings: dict, key: str) -> dict:
    value = settings.get(key, {})
    if not isinstance(value, dict):
        raise IsolationError(f'Claude {key} 정책 형식이 올바르지 않습니다.')
    return value


SOURCE_ARGUMENTS = ('--setting-sources', 'user,project,local')
SANDBOX_REQUIRED = {'enabled': True, 'failIfUnavailable': True,
                    'allowUnsandboxedCommands': False, 'autoAllowBashIfSandboxed': False,
                    'excludedCommands': [], 'allowNestedUserNamespaces': False,
                    'enableWeakerNestedSandbox': False}
NETWORK_LIMITS = {'allowAllUnixSockets': False, 'allowUnixSockets': [],
                  'allowLocalBinding': False}


def validate_settings(inherited: dict) -> None:
    """Reject inherited capability/extension conflicts before even discovery."""
    if platform.system() not in {'Linux', 'Darwin'}:
        raise IsolationError('Claude native sandbox가 없는 환경에서는 관리 호출을 지원하지 않습니다.')
    if not isinstance(inherited, dict):
        raise IsolationError('Claude managed 정책이 JSON object가 아닙니다.')
    permissions = _object(inherited, 'permissions')
    sandbox = _object(inherited, 'sandbox')
    filesystem = _object(sandbox, 'filesystem')
    network = _object(sandbox, 'network')
    auto = _object(inherited, 'autoMode')
    required = SANDBOX_REQUIRED
    # Validate without coercion: JSON 0 is not a Boolean false.
    if (any(key in sandbox and (type(sandbox[key]) is not type(value) or sandbox[key] != value)
            for key, value in required.items())
            or set(sandbox) - {*required, 'filesystem', 'network'}
            or set(filesystem) - {'allowWrite', 'denyWrite', 'allowRead', 'denyRead', 'disabled'}
            or ('disabled' in filesystem and filesystem['disabled'] is not False)):
        raise IsolationError('Claude managed sandbox 정책이 역할별 쓰기 제한과 충돌합니다.')
    if (('blockReadsOutsideWorkingDirectories' in permissions
             and permissions['blockReadsOutsideWorkingDirectories'] is not True)
            or inherited.get('disableAutoMode') or permissions.get('disableAutoMode')
            or permissions.get('defaultMode', 'auto') != 'auto'
            or permissions.get('allow') or permissions.get('additionalDirectories')
            or set(auto) - {'classifyAllShell'}
            or ('classifyAllShell' in auto and auto['classifyAllShell'] is not True)):
        raise IsolationError('Claude managed 권한 정책이 자동 승인 검토·쓰기 경계와 충돌합니다.')
    if any(inherited.get(key) for key in (
            'env', 'hooks', 'statusLine', 'subagentStatusLine', 'fileSuggestion',
            'enabledPlugins', 'managedMcpServers', 'mcpServers', 'policyHelper',
            'apiKeyHelper', 'otelHeadersHelper', 'awsAuthRefresh', 'awsCredentialExport',
            'gcpAuthRefresh')):
        raise IsolationError('Claude managed 확장·환경 정책의 쓰기 경계를 확인할 수 없습니다.')
    if set(permissions) - {'defaultMode', 'allow', 'deny', 'ask', 'additionalDirectories',
                           'disableAutoMode', 'disableBypassPermissionsMode',
                           'blockReadsOutsideWorkingDirectories'}:
        raise IsolationError('Claude managed 파일 권한 정책을 해석할 수 없습니다.')
    network_limits = NETWORK_LIMITS
    if (any(key in network and (type(network[key]) is not type(value) or network[key] != value)
            for key, value in network_limits.items())
            or set(network) - {*network_limits, 'allowedDomains', 'deniedDomains', 'strictAllowlist'}):
        raise IsolationError('Claude managed socket 정책이 sandbox 밖 실행을 허용합니다.')
    for key in ('allow', 'deny', 'ask', 'additionalDirectories'):
        if key in permissions and (not isinstance(permissions[key], list)
                or any(not isinstance(value, str) for value in permissions[key])):
            raise IsolationError('Claude managed 권한 목록 형식이 올바르지 않습니다.')
    for key in ('allowWrite', 'denyWrite', 'allowRead', 'denyRead'):
        if key in filesystem and (not isinstance(filesystem[key], list)
                or any(not isinstance(value, str) for value in filesystem[key])):
            raise IsolationError('Claude managed filesystem 목록 형식이 올바르지 않습니다.')


def policy_arguments(boundary: WorktreeBoundary, temporary: Path, read_only: bool,
                     *, fast_mode: bool = False, managed_settings: dict | None = None,
                     bash_temporary: Path | None = None, targeted_runtime: Path | None = None
                     ) -> tuple[str, ...]:
    """Compose explicit deny rules with working-directory checks and strict Bash.

    managed_settings is the caller's resolved policy from all consumed sources.
    Unknown or incompatible security settings require diagnosis;
    emitting an empty list cannot remove an inherited list.
    """
    inherited = {} if managed_settings is None else managed_settings
    validate_settings(inherited)
    filesystem = inherited.get("sandbox", {}).get("filesystem", {})
    required = SANDBOX_REQUIRED
    network_limits = NETWORK_LIMITS

    workspace, temporary = _canonical(boundary.worktree), _canonical(temporary)
    temporaries = tuple(dict.fromkeys((temporary, *(() if bash_temporary is None else
                                                       (_canonical(bash_temporary),)))))
    runtime_reads = () if targeted_runtime is None else (_canonical(targeted_runtime),)
    protected = tuple(dict.fromkeys(_canonical(path) for path in (
        *runtime_reads, boundary.source, *boundary.other_worktrees, boundary.common_dir,
        boundary.git_dir, workspace / '.git')))
    for denied in protected:
        if (workspace == denied or workspace.is_relative_to(denied)
                or any(root == denied or root.is_relative_to(denied)
                       or denied.is_relative_to(root) for root in temporaries)):
            raise IsolationError('Claude 허용 경로와 보호 경로가 겹칩니다.')
    if any(root == workspace or root.is_relative_to(workspace) or workspace.is_relative_to(root)
           for root in temporaries):
        raise IsolationError('Claude 임시 경로와 workspace가 겹칩니다.')
    writable = temporaries if read_only else (workspace, *temporaries)
    for raw in filesystem.get('allowWrite', []):
        path = _canonical(Path(raw))
        if not any(path == root or path.is_relative_to(root) for root in writable):
            raise IsolationError('Claude managed allowWrite가 허용 범위를 넓힙니다.')

    # CLAUDE_CODE_TMPDIR can fall back to a short per-user system directory.
    # Deny that default; only the owned per-call temporary root is added below.
    default_temporary = tuple(dict.fromkeys(
        base.resolve() / f'claude-{os.getuid()}'
        for base in (Path('/tmp'), Path(tempfile.gettempdir()))))
    denied_paths = (*protected, *default_temporary, *((workspace,) if read_only else ()))
    if any(root == denied or root.is_relative_to(denied) or denied.is_relative_to(root)
           for root in temporaries for denied in default_temporary):
        raise IsolationError('Claude 호출 임시 경로와 기본 공유 임시 경로가 겹칩니다.')
    deny = [rule for path in protected
            for rule in (f'Edit(/{path})', f'Edit(/{path}/**)')]
    # Native file tools run outside the Bash sandbox. Deny them before auto
    # classification; all file changes must use the sandboxed Bash tool.
    deny.append('Edit')  # Covers Edit, Write and NotebookEdit together.
    settings = {'permissions': {'defaultMode': 'auto', 'deny': deny,
                                'blockReadsOutsideWorkingDirectories': True,
                                'additionalDirectories': [str(root) for root in temporaries]},
                'autoMode': {'classifyAllShell': True}, 'disableAllHooks': True,
                'fastMode': fast_mode,
                'sandbox': dict(required, network=network_limits, filesystem={
                    'disabled': False, 'allowRead': [str(root) for root in runtime_reads],
                    'allowWrite': [str(root) for root in temporaries],
                    'denyWrite': [str(path) for path in denied_paths]})}
    tools = ['Bash', 'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch']
    return (*SOURCE_ARGUMENTS, '--permission-mode', 'auto',
            '--tools', ','.join(tools), '--disallowedTools', 'mcp__*,Edit,Write,NotebookEdit',
            '--settings', json.dumps(settings, sort_keys=True))
