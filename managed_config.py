"""Validation boundary for Loop Manager's user-managed configuration."""

from __future__ import annotations

import configparser
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
    refactoring_review_interval: int = 5
    verify_timeout_seconds: int = 600

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token)


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
        fallback="5",
    )
    try:
        refactoring_review_interval = int(raw_refactoring_review_interval)
    except ValueError as error:
        raise ManagedConfigError(
            "[loop] refactoring_review_interval은 양의 정수여야 합니다."
        ) from error
    if refactoring_review_interval <= 0:
        raise ManagedConfigError(
            "[loop] refactoring_review_interval은 양의 정수여야 합니다."
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

    return ManagedConfig(
        path=resolved,
        max_workers=max_workers,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
    )
