"""Codex/Claude command construction, output parsing and process execution."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from loop_manager_internal.runtime import operation_events as events
from typing import Protocol

from loop_manager_internal.agents.agent_invocation import AgentRequest, AgentResult, AgentResultKind, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_profiles import agent_environment, prepared_environment
from loop_manager_internal.agents.provider_home import prepare_authentication, authentication_links, assert_authentication_links
from loop_manager_internal.agents.agent_provider_usage import claude_usage, codex_log_usage
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.agents import provider_guidance
from loop_manager_internal.agents.common_guidance import CommonGuidance
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.config.managed_config import provider_fast_mode

AGENT_CLI_TIMEOUT_SECONDS = 3600
MAX_REVIEWER_LINE = 256 * 1024
MAX_REVIEWER_LOG = 2 * 1024 * 1024
CodexResult = AgentResult

@dataclass(frozen=True)
class ProviderEffects:
    """Explicit effects supplied by the composition boundary."""

    role: Callable[[Path, str, Path | None], tuple[str, str]]
    worker_profile: Callable[[Path, int, Path | None], tuple[str, str]]
    _agent_profile: Callable[[Path, str, str, int | None, Path], ResolvedProfile]
    _handoff_popen: Callable[..., subprocess.Popen[str]]
    _handoff_run: Callable[..., subprocess.CompletedProcess[str]]
    validate_runtime: Callable[..., None] | None = None


def _provider_log_directory(worktree: Path, state_dir: Path) -> Path:
    directory = state_dir / "logs" / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _provider_log_path(worktree: Path, state_dir: Path, provider: str, role: str, invocation_id=None) -> Path:
    identity = invocation_id or events.current_context().get("invocation_id")
    prefix = "claude-" if provider == "claude" else ""
    # shortcut: 호출별 로그는 자동 만료하지 않으며, 보존 용량 정책이 정해지면 정리를 추가한다.
    suffix = f"-{identity}" if identity else ""
    return _provider_log_directory(worktree, state_dir) / f"{prefix}{role}{suffix}.log"


def _json_object(line: str) -> tuple[dict[str, object] | None, bool]:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None, False
    return (event if isinstance(event, dict) else None), True


def _redact_reviewer_log(raw: str) -> str:
    """Decode JSON events so escaped credential keys receive the same scrubber."""
    def decoded_escapes(value: str) -> str:
        while True:
            decoded = re.sub(r"(?<!\\)\\u([0-9a-fA-F]{4})",
                             lambda match: chr(int(match.group(1), 16)), value)
            decoded = decoded.replace("\\\\", "\\").replace('\\"', '"')
            if decoded == value:
                return value
            value = decoded

    def scrub_text(value: str, omitted: str) -> str:
        clean = redact_secrets(value)
        if "\\" not in value:
            return clean
        if redact_secrets(decoded_escapes(value)) != decoded_escapes(clean):
            return omitted
        return clean

    def scrub(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: ("<REDACTED>" if isinstance(key, str) and
                      redact_secrets(f"{key}: sentinel") != f"{key}: sentinel"
                      else scrub(value_item))
                for key, value_item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    return json.dumps(scrub(json.loads(value)), ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
            return scrub_text(value, "[escaped reviewer text omitted]")
        return value

    lines = []
    for line in raw.splitlines(keepends=True):
        try:
            decoded = json.loads(line)
        except (ValueError, RecursionError):
            lines.append(scrub_text(line, "[malformed reviewer JSON omitted]"))
        else:
            lines.append(json.dumps(scrub(decoded), ensure_ascii=False) +
                         ("\n" if line.endswith("\n") else ""))
    return "".join(lines)


def _bounded_reviewer_lines(stream):
    """Yield JSONL as each newline arrives without retaining oversized lines."""
    pending = ""
    oversized = False
    while part := stream.readline(MAX_REVIEWER_LINE + 1):
        if not oversized:
            if len(pending) + len(part) <= MAX_REVIEWER_LINE:
                pending += part
            else:
                pending = ""
                oversized = True
        if part.endswith("\n"):
            yield None if oversized else pending
            pending = ""
            oversized = False
    if pending or oversized:
        yield None if oversized else pending


def _codex_reviewer_message(raw: str) -> str:
    """Extract the final agent message from JSON stdout."""
    message = ""
    for line in raw.splitlines():
        event, _ = _json_object(line)
        if event is None or event.get("type") not in {"item.completed", "item.updated"}:
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            message = item["text"]
    return _redact_reviewer_log(message)


def _start_timeout(process: subprocess.Popen[str], triggered: threading.Event,
                   stop: Callable[[subprocess.Popen[str]], None] | None = None) -> threading.Timer:
    def terminate() -> None:
        triggered.set()
        _stop_process(process, stop)

    timeout = threading.Timer(AGENT_CLI_TIMEOUT_SECONDS, terminate)
    timeout.daemon = True
    timeout.start()
    return timeout


def _stop_process(process: subprocess.Popen[str],
                  stop: Callable[[subprocess.Popen[str]], None] | None = None) -> None:
    try:
        if stop is None:
            process.kill()
        else:
            stop(process)
    except OSError:
        pass


def run_codex(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    resolved_profile: ResolvedProfile | None = None,
    isolation_arguments: tuple[str, ...] | None = None,
    isolation_evidence: agent_write_isolation.IsolationEvidence | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
    on_process_started: Callable[[], None] | None = None,
    on_process_finished: Callable[[], None] | None = None,
    common_guidance: CommonGuidance | None = None,
    prepared_launch: agent_write_isolation.CodexLaunch | None = None,
    on_execution_identity=None,
    effects: ProviderEffects,
) -> tuple[int, str] | CodexResult:
    codex_launch = prepared_launch
    launch_environment = (dict(prepared_launch.environment) if prepared_launch is not None
                          else agent_environment("codex", role_name, state_dir=state_dir))
    if on_isolation_checked is not None:
        checked: list[agent_write_isolation.IsolationEvidence] = []

        def observe(evidence: agent_write_isolation.IsolationEvidence) -> None:
            on_isolation_checked(evidence)
            checked.append(evidence)

        isolation_arguments = agent_write_isolation.prepare_isolation(
            "codex", role_name, worktree, state_dir, read_only, observe,
            isolation_snapshot_digest, config_path=config_path,
            expected_repository=expected_repository, session_id=session_id,
            environment_variables=launch_environment, prepared_launch=prepared_launch,
        )
        if isinstance(isolation_arguments, agent_write_isolation.CodexLaunch):
            codex_launch = isolation_arguments
            isolation_arguments = codex_launch.arguments
            launch_environment = codex_launch.environment
        if len(checked) != 1 or checked[0].status != "verified":
            raise agent_write_isolation.IsolationError("Codex 격리 증거가 없습니다.")
        isolation_evidence = checked[0]
    if isolation_arguments is not None:
        if isolation_evidence is None or isolation_snapshot_digest is None:
            raise agent_write_isolation.IsolationError("Codex 격리 증거가 없습니다.")
        agent_write_isolation.assert_current(
            isolation_evidence, worktree, state_dir, config_path,
            expected_repository, read_only, isolation_snapshot_digest,
            environment_variables=launch_environment,
        )
    if codex_launch is None:
        prepare_authentication('codex', launch_environment)
    auth_links = authentication_links('codex', launch_environment)
    with prepared_environment("codex", launch_environment):
        if isolation_evidence is not None and effects.validate_runtime is not None:
            effects.validate_runtime(config_path, 'codex', worktree)
            current_profile = effects._agent_profile(config_path, 'codex', role_name, level, worktree)
            if resolved_profile is not None and current_profile != resolved_profile:
                raise agent_write_isolation.IsolationError('준비된 실행 환경의 Agent profile이 변경됐습니다.')
            resolved_profile = current_profile
        if resolved_profile is not None:
            model, effort = resolved_profile.model, resolved_profile.effort
        elif role_name == "worker":
            if level is None:
                raise RuntimeError("Codex Worker level이 없습니다.")
            model, effort = effects.worker_profile(config_path, level, worktree)
        else:
            if level is not None:
                raise RuntimeError(f"Codex {role_name}에 Worker level을 지정할 수 없습니다.")
            model, effort = effects.role(config_path, role_name, worktree)
        fast_mode = provider_fast_mode(config_path, "codex")
        fast_arguments = [
            "-c", 'service_tier="fast"' if fast_mode else 'service_tier="default"',
            "-c", f'features.fast_mode={str(fast_mode).lower()}',
        ]
        fast_mode_log = json.dumps({"type": "loop_manager.fast_mode", "requested": fast_mode}) + "\n"

    guidance_context = tuple(isolation_arguments or codex_sandbox_arguments(read_only))
    instruction_digests = []
    additional = (provider_guidance.codex_arguments(
        common_guidance, worktree, environment=launch_environment,
        observe_instruction=instruction_digests.append,
        context_arguments=("-m", model, "-c", f'model_reasoning_effort="{effort}"',
                          *fast_arguments, *guidance_context),
    ) if common_guidance else [])
    assert_authentication_links('codex', launch_environment, auth_links)
    if codex_launch is not None and isolation_evidence is not None:
        agent_write_isolation.assert_current(isolation_evidence, worktree, state_dir, config_path,
            expected_repository, read_only, isolation_snapshot_digest, environment_variables=launch_environment)
        if on_execution_identity is not None:
            from loop_manager_internal.agents.provider_execution import execution_identity
            if not instruction_digests:
                raise agent_write_isolation.IsolationError('실제 Provider 지침 적용 증거가 없습니다.')
            on_execution_identity(execution_identity(isolation_evidence, codex_launch,
                ResolvedProfile(model, effort), instruction_digests[0], read_only=read_only))
    project_dir = _provider_log_directory(worktree, state_dir)
    last_message = project_dir / f"{role_name}-last.txt"
    saved_log = _provider_log_path(worktree, state_dir, "codex", role_name)
    log = codex_launch.attempt / "provider.log" if codex_launch else saved_log
    # Preserve the previous result until native policy preflight accepts the call.
    provider_last_message = (codex_launch.attempt / "last-message.txt"
                             if codex_launch else last_message)
    if codex_launch is None:
        last_message.unlink(missing_ok=True)
    permissions = (list(isolation_arguments) if isolation_arguments is not None
                   else codex_sandbox_arguments(read_only))
    if session_id:
        arguments = [
            "codex",
            "exec",
            *permissions,
            "-C",
            str(worktree),
            "resume",
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            *fast_arguments,
            *additional,
            *([] if role_name == "reviewer" else ["-o", str(provider_last_message)]),
            session_id,
            "-",
        ]
    else:
        arguments = [
            "codex",
            "exec",
            *(["--json"] if persist_session or role_name == "reviewer" else []),
            *(["--ephemeral"] if not persist_session else []),
            *permissions,
            "-C",
            str(worktree),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            *fast_arguments,
            *additional,
            *([] if role_name == "reviewer" else ["-o", str(provider_last_message)]),
            "-",
        ]
    resolved_session_id = session_id
    timeout_triggered = threading.Event()
    collection_status = "unknown"
    reviewer_message = ""
    reviewer_tail = ""
    reviewer_usage = ""
    if ((persist_session and not session_id) or role_name == "reviewer" or
            (codex_launch is None and (on_process_started is not None or on_process_finished is not None))):
        with log.open("w", encoding="utf-8") as output:
            output.write(fast_mode_log)
            start = codex_launch.start if codex_launch else effects._handoff_popen
            process = start(
                arguments,
                **({'launch_process': effects._handoff_popen} if codex_launch else {'stdin': subprocess.PIPE}),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=launch_environment,
                cwd=worktree,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            timeout = _start_timeout(process, timeout_triggered, codex_launch.stop if codex_launch else None)
            try:
                if on_process_started is not None:
                    on_process_started()
                process.stdin.write(prompt)
                process.stdin.close()
                lines = (_bounded_reviewer_lines(process.stdout) if role_name == "reviewer"
                         else process.stdout)
                remaining = MAX_REVIEWER_LOG
                for line in lines:
                    if role_name == "reviewer":
                        safe = ("[reviewer output line omitted]\n" if line is None
                                else _redact_reviewer_log(line))
                        reviewer_tail = (reviewer_tail + safe)[-6000:]
                        if remaining:
                            fragment = safe[:remaining]
                            output.write(fragment)
                            output.flush()
                            remaining -= len(fragment)
                        if line is None:
                            continue
                        reviewer_message = _codex_reviewer_message(line) or reviewer_message
                        event, _decoded = _json_object(line)
                        if event is not None and event.get("type") == "turn.completed":
                            reviewer_usage = safe
                    else:
                        output.write(line)
                        output.flush()
                    if resolved_session_id:
                        continue
                    if role_name != "reviewer":
                        event, _decoded = _json_object(line)
                    if event is None:
                        continue
                    if event.get("type") == "thread.started" and isinstance(
                        event.get("thread_id"), str
                    ):
                        resolved_session_id = event["thread_id"]
                        if on_session_started:
                            on_session_started(resolved_session_id)
                returncode = process.wait()
                if on_process_finished is not None:
                    on_process_finished()
            except BaseException:
                _stop_process(process, codex_launch.stop if codex_launch else None)
                process.wait()
                if on_process_finished is not None:
                    on_process_finished()
                raise
            finally:
                timeout.cancel()
                process.stdout.close()
                if codex_launch is not None:
                    codex_launch.stop(process)
    else:
        with log.open("w", encoding="utf-8") as output:
            output.write(fast_mode_log)
            runner = codex_launch.run if codex_launch is not None else effects._handoff_run
            process_callbacks = ({"on_process_started": on_process_started,
                                  "on_process_finished": on_process_finished}
                                 if codex_launch is not None else {})
            result = runner(
                arguments,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=output,
                stderr=subprocess.STDOUT,
                env=launch_environment,
                cwd=worktree,
                timeout=AGENT_CLI_TIMEOUT_SECONDS,
                **({"launch_process": effects._handoff_popen} if codex_launch is not None else {}),
                **process_callbacks,
            )
        returncode = result.returncode
    assert_authentication_links('codex', launch_environment, auth_links)
    try:
        message = (reviewer_message if role_name == "reviewer" else
                   provider_last_message.read_text(encoding="utf-8")
                   if provider_last_message.is_file() else "")
    except (OSError, UnicodeError):
        # The process has already completed; unavailable auxiliary output must
        # not turn that completed physical call into a retry.
        message = ""
        collection_status = "error"
    if role_name == "reviewer" and message:
        try:
            last_message.write_text(message, encoding="utf-8")
        except (OSError, UnicodeError):
            collection_status = "error"
    if role_name != "reviewer" and codex_launch is not None and provider_last_message.is_file():
        try:
            last_message.write_text(message, encoding="utf-8")
        except (OSError, UnicodeError):
            collection_status = "error"
    if timeout_triggered.is_set():
        message = f"Codex {role_name} 실행 timeout ({AGENT_CLI_TIMEOUT_SECONDS}초)"
    if returncode and not message:
        try:
            message = (reviewer_tail if role_name == "reviewer" else
                       log.read_text(encoding="utf-8")[-6000:])
        except (OSError, UnicodeError):
            collection_status = "error"
    if role_name == "reviewer":
        message = _redact_reviewer_log(message)
    try:
        usage = codex_log_usage(reviewer_usage if role_name == "reviewer" else
                                log.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        # Usage is observational; a failed log read cannot retry a completed CLI call.
        usage = Usage(status="invalid")
    if codex_launch is not None and returncode != 125:
        try:
            saved_log.write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
        except (OSError, UnicodeError):
            # Keep the process result and parsed usage; only the auxiliary
            # durable copy failed and the ledger records that separately.
            collection_status = "error"
    if persist_session and not resolved_session_id:
        if returncode == 0:
            return CodexResult(
                1,
                f"Codex {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
                usage=usage,
                collection_status=collection_status,
            )
    if persist_session or session_id or collection_status == "error":
        return CodexResult(
            returncode,
            message.strip(),
            resolved_session_id,
            (
                AgentResultKind.TRANSIENT_FAILURE
                if timeout_triggered.is_set()
                else None
            ),
            usage,
            collection_status,
        )
    return returncode, message.strip()


def run_claude(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    resolved_profile: ResolvedProfile | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
    on_process_started: Callable[[], None] | None = None,
    on_process_finished: Callable[[], None] | None = None,
    stream_output: bool = False,
    common_guidance: CommonGuidance | None = None,
    effects: ProviderEffects,
) -> tuple[int, str] | AgentResult:
    launch_environment = agent_environment("claude", role_name, state_dir=state_dir)
    prepare_authentication('claude', launch_environment)
    auth_links = authentication_links('claude', launch_environment)
    with prepared_environment('claude', launch_environment):
        profile = resolved_profile or effects._agent_profile(
            config_path, "claude", role_name, level, worktree
        )
    model, effort = profile.model, profile.effort
    fast_mode = provider_fast_mode(config_path, "claude")
    fast_mode_log = json.dumps({"type": "loop_manager.fast_mode", "requested": fast_mode}) + "\n"
    additional = provider_guidance.claude_arguments(common_guidance, environment=launch_environment) if common_guidance else []
    assert_authentication_links('claude', launch_environment, auth_links)
    if on_isolation_checked is not None:
        agent_write_isolation.prepare_isolation(
            "claude", role_name, worktree, state_dir, read_only,
            on_isolation_checked, isolation_snapshot_digest,
            config_path=config_path, expected_repository=expected_repository,
            session_id=session_id, environment_variables=launch_environment,
        )
    project_dir = _provider_log_directory(worktree, state_dir)
    attempt = Path(tempfile.mkdtemp(prefix=f'claude-{role_name}-', dir=project_dir))
    launch_environment['TMPDIR'] = str(attempt)
    if os.name == 'nt':
        launch_environment['TMP'] = launch_environment['TEMP'] = str(attempt)
    log = _provider_log_path(worktree, state_dir, "claude", role_name)
    last_message = project_dir / f"claude-{role_name}-last.txt"
    last_message.unlink(missing_ok=True)

    streaming = role_name == "reviewer" or (persist_session and session_id is None) or stream_output
    arguments = [
        "claude",
        "--print",
        "--output-format",
        "stream-json" if streaming else "json",
        *(["--verbose"] if streaming else []),
        "--model",
        model,
        "--effort",
        effort,
        *claude_permission_arguments(read_only, fast_mode=fast_mode),
        *additional,
        *(["--resume", session_id] if session_id else []),
    ]
    resolved_session_id = session_id
    message = ""
    returncode = 0
    timeout_triggered = threading.Event()
    usage = Usage()
    collection_status = "unknown"

    if streaming:
        final_event: Mapping[str, object] = {}
        stream_error_seen = False
        stream_error_message = ""
        diagnostic_tail = ""
        with log.open("w", encoding="utf-8") as output:
            output.write(fast_mode_log)
            process = effects._handoff_popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=launch_environment,
                cwd=worktree,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            timeout = _start_timeout(process, timeout_triggered)
            try:
                if on_process_started is not None:
                    on_process_started()
                process.stdin.write(prompt)
                process.stdin.close()
                lines = (_bounded_reviewer_lines(process.stdout) if role_name == "reviewer"
                         else process.stdout)
                remaining = MAX_REVIEWER_LOG
                for line in lines:
                    if role_name == "reviewer":
                        safe = ("[reviewer output line omitted]\n" if line is None
                                else _redact_reviewer_log(line))
                        diagnostic_tail = (diagnostic_tail + safe)[-6000:]
                        if remaining:
                            fragment = safe[:remaining]
                            output.write(fragment)
                            output.flush()
                            remaining -= len(fragment)
                        if line is None:
                            usage = Usage(status="invalid")
                            continue
                    else:
                        output.write(line)
                        output.flush()
                    event, decoded = _json_object(line)
                    if event is None:
                        if not decoded:
                            diagnostic_tail = (diagnostic_tail + (safe if role_name == "reviewer" else line))[-6000:]
                        if usage.status == "unknown":
                            usage = Usage(status="invalid")
                        continue
                    if event.get("is_error") or event.get("type") == "error":
                        stream_error_seen = True
                        candidate = _claude_result_message(event)
                        stream_error_message = candidate or (
                            safe if role_name == "reviewer" else line).strip()[-6000:]
                    if event.get("is_error") or event.get("type") in {
                        "error",
                        "result",
                    }:
                        diagnostic_tail = (diagnostic_tail + (safe if role_name == "reviewer" else line))[-6000:]
                    event_session = event.get("session_id")
                    if (
                        resolved_session_id is None
                        and isinstance(event_session, str)
                        and event_session
                    ):
                        resolved_session_id = event_session
                        if on_session_started:
                            on_session_started(resolved_session_id)
                    if event.get("type") == "result":
                        final_event = event
                        parsed_usage = claude_usage(event)
                        if parsed_usage.status != "unknown" or usage.status != "invalid":
                            usage = parsed_usage
                returncode = process.wait()
                if on_process_finished is not None:
                    on_process_finished()
            except BaseException:
                _stop_process(process)
                process.wait()
                if on_process_finished is not None:
                    on_process_finished()
                raise
            finally:
                timeout.cancel()
                process.stdout.close()
        message = stream_error_message or _claude_result_message(final_event)
        if stream_error_seen and returncode == 0:
            returncode = 1
        if returncode and not message:
            message = diagnostic_tail
    else:
        if on_process_started is not None:
            process = effects._handoff_popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=launch_environment,
                cwd=worktree,
            )
            try:
                on_process_started()
                raw_output, _ = process.communicate(prompt, timeout=AGENT_CLI_TIMEOUT_SECONDS)
                returncode = process.returncode
                if on_process_finished is not None:
                    on_process_finished()
            except BaseException:
                _stop_process(process)
                try:
                    recovered_output, _ = process.communicate()
                except BaseException:
                    recovered_output = None
                else:
                    try:
                        log.write_text(fast_mode_log + (recovered_output or ""), encoding="utf-8")
                    except OSError:
                        collection_status = "error"
                    if on_process_finished is not None:
                        on_process_finished()
                raise
            raw_output = raw_output or ""
            try:
                log.write_text(fast_mode_log + raw_output, encoding="utf-8")
            except OSError:
                collection_status = "error"
        else:
            result = effects._handoff_run(
                arguments,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=launch_environment,
                cwd=worktree,
                timeout=AGENT_CLI_TIMEOUT_SECONDS,
            )
            returncode = result.returncode
            raw_output = result.stdout or ""
            try:
                log.write_text(fast_mode_log + raw_output, encoding="utf-8")
            except OSError:
                collection_status = "error"
        try:
            event = json.loads(raw_output)
        except (ValueError, RecursionError):
            event = {}
            usage = Usage(status="invalid")
        if isinstance(event, dict):
            message = _claude_result_message(event)
            parsed_usage = claude_usage(event)
            if parsed_usage.status != "unknown" or usage.status != "invalid":
                usage = parsed_usage
            event_session = event.get("session_id")
            if isinstance(event_session, str) and event_session:
                resolved_session_id = event_session
            if event.get("is_error") and returncode == 0:
                returncode = 1
        if not message and returncode:
            message = raw_output[-6000:]

    assert_authentication_links('claude', launch_environment, auth_links)
    if timeout_triggered.is_set():
        message = f"Claude {role_name} 실행 timeout ({AGENT_CLI_TIMEOUT_SECONDS}초)"
    if role_name == "reviewer":
        message = _redact_reviewer_log(message)
    if message:
        try:
            last_message.write_text(message, encoding="utf-8")
        except OSError:
            collection_status = "error"
    if persist_session and not resolved_session_id and returncode == 0:
        return AgentResult(
            1,
            f"Claude {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
            usage=usage,
            collection_status=collection_status,
        )
    if persist_session or session_id or collection_status == "error":
        return AgentResult(
            returncode,
            message.strip(),
            resolved_session_id,
            (
                AgentResultKind.TRANSIENT_FAILURE
                if timeout_triggered.is_set()
                else None
            ),
            usage,
            collection_status,
        )
    return returncode, message.strip()


def codex_sandbox_arguments(read_only: bool) -> list[str]:
    return list(agent_write_isolation.codex_arguments(read_only))


def claude_permission_arguments(read_only: bool, *, fast_mode: bool = False) -> list[str]:
    arguments = ["--permission-mode", "auto"]
    if read_only:
        arguments.extend([
            "--disallowedTools",
            "Edit",
            "Write",
            "NotebookEdit",
        ])
    settings = {"permissions": {"defaultMode": "auto"}, "sandbox": {
        "enabled": True, "failIfUnavailable": True,
        "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
        "excludedCommands": [],
    }}
    settings["fastMode"] = fast_mode
    arguments.extend(["--settings", json.dumps(settings, sort_keys=True)])
    return arguments


def _claude_result_message(event: Mapping[str, object]) -> str:
    result = event.get("result")
    if isinstance(result, str):
        return result
    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return str(error["message"])
    return ""


class AgentProvider(Protocol):
    """Provider boundary consumed by orchestration state transitions."""

    name: str

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        ...


class CodexAgentAdapter:
    name = "codex"

    def __init__(self, run: Callable[..., tuple[int, str] | AgentResult]):
        self._run = run

    def prepare_environment(self, request: AgentRequest):
        return agent_write_isolation.prepare_execution_environment('codex', request.role_name,
            request.worktree, request.state_dir, request.read_only, request.expected_repository,
            agent_environment('codex', request.role_name, state_dir=request.state_dir))

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        return self._run(
            request.worktree,
            request.state_dir,
            request.config_path,
            request.role_name,
            request.prompt,
            read_only=request.read_only,
            level=request.level,
            persist_session=request.persist_session,
            session_id=request.session_id,
            on_session_started=request.on_session_started,
            resolved_profile=request.profile,
            isolation_snapshot_digest=request.isolation_snapshot_digest,
            expected_repository=request.expected_repository,
            on_isolation_checked=request.on_isolation_checked,
            common_guidance=request.common_guidance,
            prepared_launch=request.prepared_launch,
            on_execution_identity=request.on_execution_identity,
            on_process_started=request.on_process_started,
            on_process_finished=request.on_process_finished,
        )


class ClaudeAgentAdapter:
    name = "claude"

    def __init__(self, run: Callable[..., tuple[int, str] | AgentResult]):
        self._run = run

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        return self._run(
            request.worktree,
            request.state_dir,
            request.config_path,
            request.role_name,
            request.prompt,
            read_only=request.read_only,
            level=request.level,
            persist_session=request.persist_session,
            session_id=request.session_id,
            on_session_started=request.on_session_started,
            resolved_profile=request.profile,
            on_isolation_checked=request.on_isolation_checked,
            isolation_snapshot_digest=request.isolation_snapshot_digest,
            expected_repository=request.expected_repository,
            common_guidance=request.common_guidance,
            on_process_started=request.on_process_started,
            on_process_finished=request.on_process_finished,
            stream_output=request.stream_output,
        )
