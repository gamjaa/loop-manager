"""One sandboxed Codex process: confirm its thread policy, then start work.

The app-server protocol exposes effective start/resume settings; exec JSONL
does not. No turn or approval grant is sent before policy confirmation.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from agent_approval_policy import ApprovalPolicyError, confirm_codex, granular_policy
from agent_invocation import AgentResult, AgentResultKind, Usage


class SessionUnavailable(RuntimeError):
    pass


class PolicyChannel:
    """Bounded JSON-RPC reader; provider requests are denied, never approved."""

    def __init__(self, process):
        self.process = process
        self.messages = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.sequence = 0
        self.pending = deque()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(2 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith("\n"):
                    raise ValueError("oversized protocol message")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid protocol message")
                while not self.closed.is_set():
                    try:
                        self.messages.put(message, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            while not self.closed.is_set():
                try:
                    self.messages.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise ApprovalPolicyError("Codex 정책 확인 채널에 요청을 전달할 수 없습니다.") from error

    def receive(self, deadline, *, pending=True):
        if pending and self.pending:
            return self.pending.popleft()
        try:
            message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as error:
            raise ApprovalPolicyError("Codex 정책 확인·자동 검토 응답 시간이 초과됐습니다.") from error
        if message is None:
            raise ApprovalPolicyError("Codex 정책 확인 채널이 응답 없이 종료됐습니다.")
        if "method" in message and "id" in message:
            # Auto-review must handle eligible approvals inside the provider.
            # A request reaching this client cannot be treated as approval.
            self.send({"id": message["id"], "error": {
                "code": -32600, "message": "Loop Manager rejects external approval grants"}})
            raise ApprovalPolicyError("Codex 자동 검토 대신 외부 승인·권한 요청이 발생했습니다.")
        return message

    def request(self, method, params, *, resume=False):
        self.sequence += 1
        identity = self.sequence
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            message = self.receive(deadline, pending=False)
            if message.get("id") != identity:
                if message.get("method") == "error":
                    raise ApprovalPolicyError("Codex 실행·자동 검토가 실패하여 호출을 중단했습니다.")
                if method == "turn/start" and "method" in message:
                    if len(self.pending) >= 128:
                        raise ApprovalPolicyError("Codex 작업 시작 응답의 허용 크기를 초과했습니다.")
                    self.pending.append(message)
                continue
            if "error" in message:
                error = message["error"]
                # Only a recognized missing session permits the existing
                # session-unavailable fallback. Never classify policy errors
                # as session loss or expose the provider's raw diagnostics.
                description = error.get("message", "") if isinstance(error, dict) else ""
                if (resume and isinstance(description, str)
                        and not re.search(r"review|guardian|classifier|approval|sandbox|policy", description, re.I)
                        and re.search(
                        r"(?:thread|session|rollout).{0,80}(?:not found|does not exist|expired)",
                        description.casefold())):
                    raise SessionUnavailable("Codex session not found")
                raise ApprovalPolicyError(f"Codex {method} 정책 확인 요청이 거부됐습니다.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ApprovalPolicyError(f"Codex {method} 정책 확인 응답 형식을 지원하지 않습니다.")
            return result


def _model_available(channel, model, effort):
    cursor = None
    seen = set()
    for _ in range(32):
        result = channel.request("model/list", {"includeHidden": True, "cursor": cursor})
        models = result.get("data")
        if not isinstance(models, list):
            break
        for entry in models:
            if not isinstance(entry, dict) or entry.get("model") != model:
                continue
            levels = entry.get("supportedReasoningEfforts")
            if isinstance(levels, list) and any(isinstance(level, dict)
                    and level.get("reasoningEffort") == effort for level in levels):
                return
            break
        cursor = result.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ApprovalPolicyError("Codex 호출 계정에서 선택 모델·effort 지원을 확인할 수 없습니다.")


def _provider_failure(error, identity, usage):
    """Retain known transport/quota recovery, never guess about review errors."""
    description = error.get("message", "") if isinstance(error, dict) else ""
    if isinstance(description, str) and re.search(r"review|guardian|classifier|approval", description, re.I):
        raise ApprovalPolicyError("Codex 자동 검토가 실패하여 호출을 중단했습니다.")
    information = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if information == "usageLimitExceeded":
        return AgentResult(1, "Codex usage limit exceeded", identity,
                           AgentResultKind.USAGE_LIMIT, usage)
    if isinstance(information, str) and information in (
            "rateLimitExceeded", "serverOverloaded", "flexUnavailable", "internalServerError"):
        return AgentResult(1, f"Codex provider temporarily unavailable: {information}", identity,
                           AgentResultKind.TRANSIENT_FAILURE, usage)
    if isinstance(information, dict) and len(information) == 1:
        category, details = next(iter(information.items()))
        if category in ("httpConnectionFailed", "responseStreamConnectionFailed",
                        "responseStreamDisconnected", "responseTooManyFailedAttempts"):
            status = details.get("httpStatusCode") if isinstance(details, dict) else None
            kind = (AgentResultKind.PERMANENT_CONFIGURATION if status in (401, 403)
                    else AgentResultKind.TRANSIENT_FAILURE)
            return AgentResult(1, f"Codex provider connection failed: {category}", identity, kind, usage)
    raise ApprovalPolicyError("Codex 실행·자동 검토가 실패하여 호출을 중단했습니다.")


def _run_turn(channel, identity, prompt, timeout, log):
    # Omit overrides: the same thread's confirmed policy stays active.
    response = channel.request("turn/start", {
        "threadId": identity, "input": [{"type": "text", "text": prompt, "text_elements": []}]})
    turn = response.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise ApprovalPolicyError("Codex 작업 turn identity를 확인할 수 없습니다.")
    message = ""
    usage = Usage()
    deadline = time.monotonic() + timeout
    while True:
        event = channel.receive(deadline)
        method, params = event.get("method"), event.get("params")
        if not isinstance(params, dict) or params.get("threadId") != identity:
            continue
        if params.get("turnId", turn_id) != turn_id:
            continue
        if method in ("item/completed", "turn/completed", "error", "thread/tokenUsage/updated"):
            log.write(json.dumps(event) + "\n")
            log.flush()
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str):
                    message = text
        elif method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage")
            counts = token_usage.get("last") if isinstance(token_usage, dict) else None
            if isinstance(counts, dict):
                values = [counts.get(key) for key in ("inputTokens", "cachedInputTokens",
                                                     "outputTokens", "reasoningOutputTokens")]
                if all(value is None or (type(value) is int and value >= 0) for value in values):
                    usage = Usage(values[0], values[1], output_tokens=values[2],
                                  reasoning_output_tokens=values[3])
        elif method == "error":
            failure = _provider_failure(params.get("error"), identity, usage)
            if params.get("willRetry") is not True:
                return failure
        elif method == "turn/completed":
            completed = params.get("turn")
            if not isinstance(completed, dict) or completed.get("id") != turn_id:
                raise ApprovalPolicyError("Codex 완료 turn identity가 일치하지 않습니다.")
            if completed.get("status") != "completed" or completed.get("error") is not None:
                return _provider_failure(completed.get("error"), identity, usage)
            return AgentResult(0, message.strip(), identity, usage=usage)


def run_reviewed(launch, *, worktree: Path, model: str, effort: str, prompt: str,
                 read_only: bool, session_id: str | None, persist_session: bool,
                 spawn, confirm, on_session_started, timeout: int) -> AgentResult:
    """Check the actual sandboxed process and session before supplying prompt."""
    # app-server accepts -c/strict-config, not exec's --sandbox argument.
    settings = list(launch.arguments)
    if settings[:2] != ["--sandbox", "read-only" if read_only else "workspace-write"]:
        raise ApprovalPolicyError("Codex 호출 sandbox 설정이 검증한 경계와 다릅니다.")
    arguments = launch.wrap(["codex", "app-server", "--stdio", *settings[2:],
                             "-c", "model=" + json.dumps(model),
                             "-c", "model_reasoning_effort=" + json.dumps(effort)])
    channel = None
    with (launch.attempt / "policy-diagnostic.log").open("w", encoding="utf-8") as diagnostic:
        process = spawn(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=diagnostic, text=True, encoding="utf-8", errors="replace",
                        env=launch.environment, cwd=worktree, start_new_session=True)
        try:
            channel = PolicyChannel(process)
            channel.request("initialize", {"clientInfo": {"name": "loop-manager", "version": "1"},
                                           "capabilities": {"experimentalApi": True}})
            channel.send({"method": "initialized", "params": {}})
            effective = channel.request("config/read", {"cwd": str(worktree), "includeLayers": False})
            config = effective.get("config")
            if not isinstance(config, dict):
                raise ApprovalPolicyError("Codex 병합된 유효 설정을 확인할 수 없습니다.")
            account = channel.request("account/read", {"refreshToken": False})
            _model_available(channel, model, effort)
            params = {"cwd": str(worktree), "model": model,
                      "approvalPolicy": granular_policy(), "approvalsReviewer": "auto_review",
                      "sandbox": "read-only" if read_only else "workspace-write"}
            if session_id:
                params.update(threadId=session_id, excludeTurns=True)
                actual = channel.request("thread/resume", params, resume=True)
            else:
                params.update(ephemeral=not persist_session, allowProviderModelFallback=False)
                actual = channel.request("thread/start", params)
            confirmation = confirm_codex(config, actual, account, model=model, effort=effort,
                worktree=worktree, temporary=launch.attempt, read_only=read_only, session_id=session_id)
            confirm(confirmation)
            if on_session_started is not None:
                on_session_started(confirmation.session_id)
            with (launch.attempt / "provider.log").open("w", encoding="utf-8") as log:
                return _run_turn(channel, confirmation.session_id, prompt, timeout, log)
        except SessionUnavailable:
            return AgentResult(1, "Codex session not found", session_id,
                               AgentResultKind.PERMANENT_CONFIGURATION)
        finally:
            if channel is not None:
                channel.closed.set()
            launch.stop(process)
            process.wait()
            if channel is not None:
                channel.reader.join(timeout=1)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
