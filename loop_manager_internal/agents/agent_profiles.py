"""호출에 필요한 시스템 환경과 선택 Provider 인증만 전달한다."""
from collections.abc import Mapping
import os
from pathlib import Path


def agent_environment(
    provider: str = "codex", role_name: str | None = None,
    *, environment_variables: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
) -> dict[str, str]:
    """Return the filtered environment for one Agent subprocess role."""
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPYCACHEPREFIX",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    provider_allowed = {
        "codex": {
            "CODEX_ACCESS_TOKEN",
            "CODEX_CA_CERTIFICATE",
            "CODEX_HOME",
            "OPENAI_API_KEY",
        },
        "claude": {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        },
    }
    if provider not in provider_allowed:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    allowed.update(provider_allowed[provider])

    source = environment_variables if environment_variables is not None else os.environ
    environment = {key: value for key, value in source.items() if key.upper() in allowed}
    # Preserve the prepared launch's owned temporary directory as well.
    if 'TMPDIR' in source:
        environment['TMPDIR'] = source['TMPDIR']
    if state_dir is not None:
        variable = 'CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'
        environment = {key: value for key, value in environment.items() if key.upper() != variable}
        environment[variable] = str(state_dir.expanduser().resolve() / f'automation-{provider}')
    return environment

