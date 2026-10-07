"""Canonical runtime paths, durable ownership intents and external CLI paths."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import RuntimeWorktree, WorkspaceOwnershipError


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _runtime_intent_path(state_dir: Path, mutation_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", mutation_id):
        raise RuntimeError("runtime mutation ID 형식이 유효하지 않습니다.")
    return state_dir / "runtime-intents" / f"{mutation_id}.json"


def _runtime_intent_fields(source: Path, runtime: Path, sha: str) -> dict[str, str]:
    return {
        "source": str(_canonical_path(source)),
        "path": str(_canonical_path(runtime)),
        "sha": sha,
    }


def _write_runtime_intent(
    state_dir: Path, mutation_id: str, source: Path, runtime: Path, sha: str,
    *, created: bool, lock_reason: str | None,
) -> None:
    path = _runtime_intent_path(state_dir, mutation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    payload = {
        **_runtime_intent_fields(source, runtime, sha),
        "created": created,
        "lock_reason": lock_reason,
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _read_runtime_intent(state_dir, mutation_id, source, runtime, sha) == (
            created, lock_reason,
        ):
            return
        raise RuntimeError("runtime mutation intent가 기존 기록과 다릅니다.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read_runtime_intent(
    state_dir: Path, mutation_id: str, source: Path, runtime: Path, sha: str,
) -> tuple[bool, str | None]:
    try:
        payload = json.loads(_runtime_intent_path(state_dir, mutation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("runtime mutation intent를 확인할 수 없습니다.") from error
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in _runtime_intent_fields(source, runtime, sha).items()
    ) or type(payload.get("created")) is not bool or (
        payload.get("lock_reason") is not None
        and not isinstance(payload.get("lock_reason"), str)
    ):
        raise RuntimeError("runtime mutation intent의 repository·경로·SHA가 다릅니다.")
    return payload["created"], payload.get("lock_reason")


def _legacy_runtime_adoption_path(state_dir: Path, source: Path, sha: str) -> Path:
    return state_dir / "runtime-intents" / "legacy" / runtime_repository_id(source) / f"{sha}.json"


def _validate_legacy_runtime_adoption(
    state_dir: Path, source: Path, runtime: Path, sha: str,
) -> None:
    try:
        payload = json.loads(
            _legacy_runtime_adoption_path(state_dir, source, sha).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise WorkspaceOwnershipError(
            f"runtime의 구버전 전환 소유 증거가 없습니다: {runtime}"
        ) from error
    if payload != {**_runtime_intent_fields(source, runtime, sha), "legacy": True}:
        raise WorkspaceOwnershipError(
            f"runtime의 구버전 전환 소유 증거가 다릅니다: {runtime}"
        )


def _record_legacy_runtime_adoption(
    state_dir: Path, source: Path, runtime: Path, sha: str,
) -> None:
    path = _legacy_runtime_adoption_path(state_dir, source, sha)
    if path.exists():
        _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_runtime_intent_fields(source, runtime, sha), "legacy": True}
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def runtime_repository_id(source: Path) -> str:
    return hashlib.sha256(str(_canonical_path(source)).encode()).hexdigest()[:16]


def expected_runtime_worktree(
    state_dir: Path,
    source: Path,
    commit_sha: str,
    *, command_id: str | None = None,
) -> Path:
    root = state_dir / "runtimes" / runtime_repository_id(source)
    if command_id is not None:
        _runtime_intent_path(state_dir, command_id)  # Validate the path component.
        root = root / "commands" / command_id
    return root / commit_sha


def source_config_path(source: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = source / candidate
    return candidate.resolve()


def runtime_child_arguments(
    arguments: list[str],
    *,
    source: Path,
    state_dir: Path,
    config_path: Path,
    runtime: RuntimeWorktree,
) -> list[str]:
    """Preserve user CLI semantics while pinning external path inputs."""

    stripped: list[str] = []
    skip_value = False
    value_options = {
        "--state-dir",
        "--config",
        "--runtime-source",
        "--runtime-sha",
        "--runtime-mutation-id",
    }
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in value_options:
            skip_value = True
            continue
        if any(argument.startswith(option + "=") for option in value_options):
            continue
        if argument == "--runtime-child":
            continue
        stripped.append(argument)
    if skip_value:
        raise RuntimeError("경로 옵션 값이 없습니다.")
    child_arguments = [
        "--runtime-child",
        "--runtime-source",
        str(source),
        "--runtime-sha",
        runtime.commit_sha,
    ]
    if runtime.mutation_id is not None:
        child_arguments.extend(("--runtime-mutation-id", runtime.mutation_id))
    return [
        *child_arguments,
        "--state-dir",
        str(state_dir),
        "--config",
        str(config_path),
        *stripped,
    ]
