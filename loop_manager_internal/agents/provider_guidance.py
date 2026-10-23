"""Provider additional-instruction preflight and lossless serialization."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import subprocess
import tomllib
from uuid import uuid4

from loop_manager_internal.agents.common_guidance import CommonGuidance, GuidanceError, _read
from loop_manager_internal.agents.agent_profiles import agent_environment


def _probe(arguments: list[str], **context) -> str:
    if context.get('env') is None:
        context['env'] = agent_environment(arguments[0])
    try:
        result = subprocess.run(arguments, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, check=False, **context)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise GuidanceError("Provider 추가 지침 기능을 확인할 수 없습니다.") from error
    if result.returncode:
        raise GuidanceError("Provider 추가 지침 기능 조회가 실패했습니다.")
    return result.stdout


def _validate(guidance: CommonGuidance) -> None:
    contents = _read(guidance.instruction_path)
    if (contents.decode("utf-8") != guidance.content
            or hashlib.sha256(contents).hexdigest() != guidance.content_hash):
        raise GuidanceError("Provider 실행 전 공통 지침 실행용 파일이 변경됐습니다.")


def _developer_texts(arguments: list[str], worktree: Path,
                     environment: dict[str, str] | None) -> list[str]:
    # Native resolution includes trusted projects, profiles and cloud/MDM layers.
    # This ephemeral prompt inspection does not enter run_turn or generate output.
    try:
        items = json.loads(_probe(arguments, cwd=worktree, env=environment))
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("expected native prompt item list")
        texts = []
        for item in items:
            if item.get("role") != "developer":
                continue
            if item.get("type") == "additional_tools":
                if not isinstance(item.get("tools"), list) or any(not isinstance(tool, dict) for tool in item["tools"]):
                    raise ValueError("unsupported developer tools")
                # Transport IDs vary between probes; preserve the actual tool definitions.
                texts.append(json.dumps({key: value for key, value in item.items() if key != "id"},
                                        ensure_ascii=False, sort_keys=True))
                continue
            if item.get("type") != "message" or not isinstance(item.get("content"), list):
                raise ValueError("unsupported developer message")
            for content in item["content"]:
                if (not isinstance(content, dict) or content.get("type") != "input_text"
                        or not isinstance(content.get("text"), str)):
                    raise ValueError("unsupported developer content")
                texts.append(content["text"])
        return texts
    except (ValueError, RecursionError) as error:
        raise GuidanceError("Codex native 추가 지침 입력을 확인할 수 없습니다.") from error


def _override(content: str) -> list[str]:
    # JSON basic strings without ASCII surrogate escapes round-trip through TOML.
    encoded = json.dumps(content, ensure_ascii=False)
    if tomllib.loads("developer_instructions=" + encoded)["developer_instructions"] != content:
        raise GuidanceError("Codex 추가 지침을 TOML로 보존할 수 없습니다.")
    return ["-c", "developer_instructions=" + encoded]


def codex_arguments(guidance: CommonGuidance, worktree: Path, *,
                    context_arguments: tuple[str, ...] = (),
                    environment: dict[str, str] | None = None,
                    observe_instruction=None) -> list[str]:
    _validate(guidance)
    version = _probe(["codex", "--version"], env=environment).strip()
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (0, 159, 3):
        raise GuidanceError("Codex CLI가 지원되는 developer_instructions 버전이 아닙니다.")
    # Exec-only switches are irrelevant to inspecting the resolved prompt.
    context = [arg for arg in context_arguments if arg not in ("--strict-config", "--ignore-rules")]
    command = ["codex", "-C", str(worktree), *context, "debug", "prompt-input"]
    baseline = _developer_texts(command, worktree, environment)
    sentinel = "loop-manager-guidance-" + uuid4().hex
    projected = _developer_texts(command[:1] + _override(sentinel) + command[1:], worktree, environment)
    positions = [index for index, text in enumerate(projected) if sentinel in text]
    if len(positions) != 1 or projected[positions[0]].count(sentinel) != 1:
        raise GuidanceError("Codex 관리 설정이 공통 추가 지침을 덮어쓰거나 native 입력이 불명확합니다.")
    index = positions[0]
    prefix, suffix = projected[index].split(sentinel)
    without = projected[:index] + projected[index + 1:]
    existing = ""
    if baseline != without:
        if len(baseline) != len(projected) or baseline[:index] + baseline[index + 1:] != without:
            raise GuidanceError("Codex 기존 추가 지침과 기본 지침을 분리해 보존할 수 없습니다.")
        original = baseline[index]
        if (not original.startswith(prefix) or not original.endswith(suffix)
                or len(original) < len(prefix) + len(suffix)):
            raise GuidanceError("Codex 기존 추가 지침의 native 형식이 불명확합니다.")
        existing = original[len(prefix):len(original) - len(suffix) if suffix else None]
    combined = (existing + "\n\n" if existing else "") + guidance.content
    override = _override(combined)
    verified = _developer_texts(command[:1] + override + command[1:], worktree, environment)
    expected = projected.copy()
    expected[index] = prefix + combined + suffix
    if verified != expected:
        raise GuidanceError("Codex 공통 추가 지침의 실제 적용과 기존 지침 보존을 확인할 수 없습니다.")
    if observe_instruction is not None:
        observe_instruction(hashlib.sha256(json.dumps(verified, ensure_ascii=False).encode()).hexdigest())
    return override


def claude_arguments(guidance: CommonGuidance, *, environment: dict[str, str] | None = None) -> list[str]:
    _validate(guidance)
    help_text = _probe(["claude", "--help"], env=environment)
    if any(flag not in help_text for flag in (
        "--append-system-prompt-file", "--system-prompt-snapshot",
    )):
        raise GuidanceError("Claude CLI의 추가 지침 파일·system prompt snapshot 기능이 없습니다.")
    return ["--append-system-prompt-file", str(guidance.instruction_path),
            "--system-prompt-snapshot", "on"]
