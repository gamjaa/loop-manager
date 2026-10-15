"""Validation boundary for Loop Manager's user-managed configuration."""

from __future__ import annotations

import configparser
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class ManagedConfigError(RuntimeError):
    """Raised when config.ini cannot safely be used by the runtime."""


@dataclass(frozen=True)
class ManagedConfig:
    path: Path
    max_workers: int
    telegram_token: str
    telegram_chat_id: str
    refactoring_review_interval: int = 20
    verify_timeout_seconds: int = 600
    verify_parallelism: str | int = "auto"
    verify_partial: bool = True
    fast_mode_values: tuple[tuple[str, str], ...] = ()

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token)


fast_mode_settings: ContextVar[ManagedConfig | None] = ContextVar("agent_fast_mode_settings", default=None)


@contextmanager
def fast_mode_scope(config: ManagedConfig):
    current = fast_mode_settings.get()
    if current is not None and current.path.resolve() == config.path.resolve():
        yield
        return
    token = fast_mode_settings.set(config)
    try:
        yield
    finally:
        fast_mode_settings.reset(token)


def provider_fast_mode(path: Path, provider: str) -> bool:
    if provider not in {"codex", "claude"}:
        raise ManagedConfigError(f"지원하지 않는 Agent provider입니다: {provider}")
    snapshot = fast_mode_settings.get()
    if snapshot is not None and snapshot.path.resolve() == path.expanduser().resolve():
        raw = dict(snapshot.fast_mode_values).get(provider, "")
    else:
        # profile을 직접 지정한 단독 호출은 설정 파일 없이도 Fast off를 기본으로 사용한다.
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, UnicodeError, configparser.Error) as error:
            raise ManagedConfigError(f"설정 파일 형식이 올바르지 않습니다: {path}") from error
        raw = parser._sections.get(provider, {}).get("fast_mode", "")
    normalized = raw.strip().lower()
    if not normalized:
        return False
    try:
        return configparser.ConfigParser.BOOLEAN_STATES[normalized]
    except KeyError as error:
        raise ManagedConfigError(f"[{provider}] fast_mode는 boolean 또는 공란이어야 합니다.") from error


def _setting(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    *,
    fallback: str,
) -> str:
    try:
        return parser.get(section, option, raw=True, fallback=fallback).strip()
    except (configparser.Error, ValueError) as error:
        raise ManagedConfigError(
            f"설정 항목 형식이 올바르지 않습니다: [{section}] {option}"
        ) from error


def load_managed_config(path: Path) -> ManagedConfig:
    """Load global settings without including configured values in errors."""

    resolved = path.expanduser().resolve()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        loaded = parser.read(resolved, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error) as error:
        raise ManagedConfigError(
            f"설정 파일 형식이 올바르지 않습니다: {resolved}"
        ) from error
    if not loaded:
        raise ManagedConfigError(
            f"설정 파일이 없습니다: {resolved}. "
            "config/config.ini.example을 config/config.ini로 복사해 설정하세요."
        )

    raw_max_workers = _setting(
        parser,
        "loop",
        "max_workers",
        fallback="3",
    )
    try:
        max_workers = int(raw_max_workers)
    except ValueError as error:
        raise ManagedConfigError(
            "[loop] max_workers는 양의 정수여야 합니다."
        ) from error
    if max_workers <= 0:
        raise ManagedConfigError("[loop] max_workers는 양의 정수여야 합니다.")

    raw_refactoring_review_interval = _setting(
        parser,
        "loop",
        "refactoring_review_interval",
        fallback="20",
    )
    try:
        refactoring_review_interval = int(raw_refactoring_review_interval)
    except ValueError as error:
        raise ManagedConfigError(
            "[loop] refactoring_review_interval은 0 이상의 정수여야 합니다."
        ) from error
    if refactoring_review_interval < 0:
        raise ManagedConfigError(
            "[loop] refactoring_review_interval은 0 이상의 정수여야 합니다."
        )

    raw_verify_timeout = _setting(
        parser, "loop", "verify_timeout_seconds", fallback="600",
    )
    if not raw_verify_timeout.isascii() or not raw_verify_timeout.isdecimal():
        raise ManagedConfigError(
            "[loop] verify_timeout_seconds는 양의 정수여야 합니다."
        )
    verify_timeout_seconds = int(raw_verify_timeout)
    if verify_timeout_seconds <= 0:
        raise ManagedConfigError(
            "[loop] verify_timeout_seconds는 양의 정수여야 합니다."
        )

    from loop_manager_internal.verification.resources import parse_parallelism
    try:
        verify_parallelism = parse_parallelism(_setting(parser, "loop", "verify_parallelism", fallback="auto"))
    except ValueError as error:
        raise ManagedConfigError("[loop] verify_parallelism은 auto 또는 양의 정수여야 합니다.") from error

    telegram_token = _setting(
        parser,
        "telegram",
        "token",
        fallback="",
    )
    telegram_chat_id = _setting(
        parser,
        "telegram",
        "chat_id",
        fallback="",
    )
    if bool(telegram_token) != bool(telegram_chat_id):
        raise ManagedConfigError(
            "[telegram] token과 chat_id는 둘 다 비우거나 둘 다 설정해야 합니다."
        )

    raw_partial = _setting(parser, "loop", "verify_partial", fallback="true").lower()
    if raw_partial not in configparser.ConfigParser.BOOLEAN_STATES:
        raise ManagedConfigError("[loop] verify_partial은 boolean이어야 합니다.")

    return ManagedConfig(
        path=resolved,
        max_workers=max_workers,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
        verify_parallelism=verify_parallelism,
        verify_partial=configparser.ConfigParser.BOOLEAN_STATES[raw_partial],
        fast_mode_values=tuple((provider, parser._sections.get(provider, {}).get("fast_mode", ""))
                               for provider in ("codex", "claude")),
    )
