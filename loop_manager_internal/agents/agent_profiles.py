"""Provider profile validation and filtered, non-secret CLI environment."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import configparser
import os
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
import re

from loop_manager_internal.agents.agent_invocation import AgentProfile


def provider_profile(
    config_path: Path,
    provider: str,
    section: str,
    name: str,
    label: str,
    model_efforts: Mapping[str, frozenset[str]],
) -> tuple[str, str]:
    """Resolve and validate one provider profile without invoking its CLI."""

    provider_labels = {"codex": "Codex", "claude": "Claude"}
    provider_label = provider_labels.get(provider)
    if provider_label is None or not section.startswith(f"{provider}."):
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(
            f"{provider_label} 설정 파일 형식이 올바르지 않습니다: {config_path}"
        ) from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if section not in parser:
        raise RuntimeError(f"{label} 설정 section [{section}]이 없습니다.")
    # Older configurations share the Planner profile. An explicitly supplied
    # maintenance profile, including an empty/invalid one, must be validated.
    if section == f"{provider}.roles" and name == "maintenance_planner" and name not in parser[section]:
        name = "planner"
    if name not in parser[section]:
        raise RuntimeError(f"{label} 설정이 없습니다: [{section}] {name}")

    try:
        value = parser[section][name]
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
        ) from error
    if not value.strip():
        raise RuntimeError(f"{label} Model alias/Effort 설정이 비어 있습니다.")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"{label} 설정은 `Model alias,Effort` 형식이어야 합니다.")
    alias, effort = parts
    if not alias:
        raise RuntimeError(f"{label} Model alias 설정이 비어 있습니다.")
    if not effort:
        raise RuntimeError(f"{label} Effort 설정이 비어 있습니다.")
    models_section = f"{provider}.models"
    if models_section not in parser:
        raise RuntimeError(
            f"{label} Model alias section [{models_section}]가 없습니다."
        )
    if alias not in parser[models_section]:
        raise RuntimeError(
            f"{label} Model alias가 [{models_section}]에 없습니다: {alias}"
        )
    try:
        model = parser[models_section][alias].strip()
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: "
            f"[{models_section}] {alias}"
        ) from error
    if not model:
        raise RuntimeError(
            f"{label} Model 설정이 비어 있습니다: [{models_section}] {alias}"
        )

    if section == f"{provider}.worker" and name == "level8":
        expected_alias, expected_effort = {
            "codex": ("frontier", "low"),
            "claude": ("strong", "max"),
        }[provider]
        if (alias, effort) != (expected_alias, expected_effort):
            raise RuntimeError(
                f"{label}은 {expected_alias},{expected_effort} 설정이어야 합니다."
            )
        if provider == "codex" and model != "gpt-6-astra":
            raise RuntimeError(f"{label} Model은 gpt-6-astra여야 합니다: {model}")
        if provider == "claude" and not re.fullmatch(
            r"claude-opus-5(?:[-.].*)?", model
        ):
            raise RuntimeError(
                f"{label} Model은 claude-opus-5 계열이어야 합니다: {model}"
            )

    supported = model_efforts.get(model)
    if supported is None:
        raise RuntimeError(
            f"{label} Model을 {provider_label} CLI가 지원하지 않습니다: {model}"
        )
    if effort not in supported:
        raise RuntimeError(
            f"{label} Model/Effort 조합을 {provider_label} CLI가 지원하지 않습니다: "
            f"{model},{effort}"
        )
    return model, effort


def provider_worker_profile(
    config_path: Path,
    provider: str,
    level: int,
    model_efforts: Mapping[str, frozenset[str]],
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 8:
        raise RuntimeError(f"{provider.capitalize()} Worker level이 유효하지 않습니다: {level}")
    name = f"level{level}"
    return provider_profile(
        config_path,
        provider,
        f"{provider}.worker",
        name,
        f"{provider.capitalize()} Worker {name}",
        model_efforts,
    )


def validate_provider_profiles(
    config_path: Path,
    provider: str,
    model_efforts: Mapping[str, frozenset[str]],
) -> None:
    """Validate every configured role before an Agent state transition."""

    for role_name in ("planner", "maintenance_planner", "reviewer"):
        provider_profile(
            config_path,
            provider,
            f"{provider}.roles",
            role_name,
            f"{provider.capitalize()} {role_name}",
            model_efforts,
        )
    for level in range(1, 9):
        provider_worker_profile(config_path, provider, level, model_efforts)


def agent_environment(
    provider: str = "codex", role_name: str | None = None
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

    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    return environment


AGENT_PROVIDERS = frozenset({'codex', 'claude'})
CLAUDE_EFFORTS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})

@dataclass(frozen=True)
class ProfileEffects:
    """Explicit effects supplied by the composition boundary."""

    command: Callable[..., subprocess.CompletedProcess[str]]
    role: Callable[[Path, str, Path | None], tuple[str, str]]
    worker_profile: Callable[[Path, int, Path | None], tuple[str, str]]
    claude_role: Callable[[Path, str, Path | None], tuple[str, str]]
    claude_worker_profile: Callable[[Path, int, Path | None], tuple[str, str]]
    codex_model_efforts: Callable[[Path | None], dict[str, frozenset[str]]]
    claude_model_efforts: Callable[[Path, Path | None], dict[str, frozenset[str]]]


def codex_model_efforts(worktree: Path | None=None, *, effects: ProfileEffects) -> dict[str, frozenset[str]]:
    try:
        catalog = json.loads(
            effects.command(
                ["codex", "debug", "models"],
                cwd=worktree,
                env=agent_environment(),
            ).stdout
        )
        models = catalog["models"]
        return {
            model["slug"]: frozenset(level["effort"] for level in model["supported_reasoning_levels"])
            for model in models
        }
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex CLI model catalog 형식이 올바르지 않습니다.") from error


def claude_model_efforts(
    config_path: Path,
    worktree: Path | None = None,
    *,
    effects: ProfileEffects,
) -> dict[str, frozenset[str]]:
    """Build a catalog from configured profiles that the Claude CLI accepts."""

    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(
            f"Claude 설정 파일 형식이 올바르지 않습니다: {config_path}"
        ) from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if "claude.models" not in parser:
        raise RuntimeError("Claude Model alias section [claude.models]가 없습니다.")

    aliases: dict[str, str] = {}
    try:
        configured_models = parser["claude.models"].items()
        for alias, model in configured_models:
            resolved = model.strip()
            if not re.fullmatch(
                r"claude-(?:opus|sonnet|haiku)-[a-z0-9.-]+", resolved
            ):
                raise RuntimeError(
                    "Claude CLI가 지원하는 Model 이름 형식이 아닙니다: "
                    f"{resolved or '<empty>'}"
                )
            aliases[alias] = resolved
    except configparser.InterpolationError as error:
        raise RuntimeError("Claude Model alias 설정 값 형식이 올바르지 않습니다.") from error

    configured_pairs: set[tuple[str, str]] = set()
    for section in ("claude.roles", "claude.worker"):
        if section not in parser:
            continue
        for name in parser[section]:
            try:
                parts = [
                    part.strip() for part in parser[section][name].split(",")
                ]
            except configparser.InterpolationError as error:
                raise RuntimeError(
                    f"Claude 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
                ) from error
            if len(parts) != 2:
                continue
            alias, effort = parts
            model = aliases.get(alias)
            if model is None or not effort:
                continue
            if effort not in CLAUDE_EFFORTS:
                raise RuntimeError(
                    "Claude Model/Effort 조합을 Claude CLI가 지원하지 않습니다: "
                    f"{model},{effort}"
                )
            configured_pairs.add((model, effort))

    environment = agent_environment("claude")
    for model in sorted(set(aliases.values())):
        result = effects.command(
            ["claude", "--bare", "--init-only", "--model", model],
            cwd=worktree,
            check=False,
            env=environment,
        )
        if result.returncode:
            raise RuntimeError(f"Claude CLI가 Model을 지원하지 않습니다: {model}")

    catalog: dict[str, set[str]] = {
        model: set() for model in set(aliases.values())
    }
    for model, effort in sorted(configured_pairs):
        result = effects.command(
            [
                "claude",
                "--bare",
                "--init-only",
                "--model",
                model,
                "--effort",
                effort,
            ],
            cwd=worktree,
            check=False,
            env=environment,
        )
        if result.returncode:
            raise RuntimeError(
                "Claude CLI가 Model/Effort 조합을 지원하지 않습니다: "
                f"{model},{effort}"
            )
        catalog[model].add(effort)

    return {
        model: frozenset(efforts) for model, efforts in catalog.items()
    }


def _agent_profile(
    config_path: Path,
    provider: str,
    role_name: str,
    level: int | None,
    worktree: Path,
    *,
    effects: ProfileEffects,
) -> AgentProfile:
    if role_name == "worker":
        if level is None:
            raise RuntimeError(f"{provider.capitalize()} Worker level이 없습니다.")
        if provider == "codex":
            return AgentProfile(*effects.worker_profile(config_path, level, worktree))
        if provider == "claude":
            return AgentProfile(
                *effects.claude_worker_profile(config_path, level, worktree)
            )
    elif level is not None:
        raise RuntimeError(
            f"{provider.capitalize()} {role_name}에 Worker level을 지정할 수 없습니다."
        )
    elif provider == "codex":
        return AgentProfile(*effects.role(config_path, role_name, worktree))
    elif provider == "claude":
        return AgentProfile(*effects.claude_role(config_path, role_name, worktree))
    raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")


def validate_provider_runtime(
    config_path: Path,
    provider: str,
    worktree: Path | None = None,
    *,
    effects: ProfileEffects,
) -> None:
    """Fail fast when the selected CLI, auth, or profiles are unavailable."""

    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    executable = "codex" if provider == "codex" else "claude"
    label = "Codex" if provider == "codex" else "Claude"
    if not shutil.which(executable):
        raise RuntimeError(f"선택한 {label} CLI가 PATH에 설치되어 있지 않습니다.")

    auth_arguments = (
        ["codex", "login", "status"]
        if provider == "codex"
        else ["claude", "auth", "status"]
    )
    auth = effects.command(
        auth_arguments,
        cwd=worktree,
        check=False,
        env=agent_environment(provider),
    )
    if auth.returncode:
        login = "codex login" if provider == "codex" else "claude auth login"
        raise RuntimeError(
            f"선택한 {label} CLI가 인증되어 있지 않습니다. {login}을 실행하세요."
        )

    try:
        model_efforts = (
            effects.codex_model_efforts(worktree)
            if provider == "codex"
            else effects.claude_model_efforts(config_path, worktree)
        )
        validate_provider_profiles(config_path, provider, model_efforts)
    except RuntimeError as error:
        raise RuntimeError(f"{label} profile 설정 오류: {error}") from error
