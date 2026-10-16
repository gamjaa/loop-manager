"""한 Manager 실행이 소유하는 예약과 인증된 loopback 통신."""
from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sys
import threading
import time

from loop_manager_internal.verification.resources import (
    DEFAULT_WORKER_BYTES, RESERVATION_GRACE, ResourceSampler, decide,
)


@dataclass(frozen=True)
class ExecutionSettings:
    coordinator: VerifyCoordinator
    cap: str | int
    state_dir: Path


settings: ContextVar[ExecutionSettings | None] = ContextVar("verify_execution_settings", default=None)


@contextmanager
def execution_settings(coordinator, cap, state_dir):
    token = settings.set(ExecutionSettings(coordinator, cap, state_dir))
    try:
        yield
    finally:
        settings.reset(token)


def process_identity(pid):
    if sys.platform == "darwin":
        from .darwin_process import process_info
        info = process_info(pid)
        return info[1] if info else None
    if os.name == "nt":
        from loop_manager_internal.execution.windows_job import process_identity as windows_identity
        return windows_identity(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    if len(fields) < 20:
        raise RuntimeError("자식 프로세스 identity 해석 실패")
    return None if fields[0] in {"Z", "X"} else int(fields[19])


def descendant(pid, root):
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == root:
            return True
        seen.add(pid)
        if sys.platform == "darwin":
            from .darwin_process import process_info
            info = process_info(pid)
            if info is None:
                return False
            pid = info[0]
            continue
        stat = Path(f"/proc/{pid}/stat").read_text()
        pid = int(stat[stat.rfind(")") + 2:].split()[1])
    return False


class VerifyCoordinator:
    def __init__(self, *, sampler=None, clock=time.monotonic, identity=process_identity):
        self.sampler = sampler or ResourceSampler(clock=clock)
        self.clock, self.identity = clock, identity
        self.lock = threading.Lock()
        self.sessions, self.reservations, self.waiters, self.last_request = {}, {}, deque(), {}
        self.server = self.thread = None

    def open(self, cap, *, require_guard=False):
        session, secret = secrets.token_hex(16), secrets.token_hex(32)
        with self.lock:
            self.sessions[session] = {"secret": secret, "cap": cap, "guard": None, "require_guard": require_guard,
                "waiting": False, "waiting_seconds": 0.0, "waiting_at": self.clock(),
                "budget_directory": os.environ.get("LOOP_MANAGER_VERIFY_BUDGET_DIR")}
        return session, secret

    def track_waiting(self, session, directory):
        with self.lock:
            self.sessions[session]["budget_directory"] = str(directory)

    def _record_wait(self, session, waiting):
        owner = self.sessions[session]
        now = self.clock()
        interval = now-owner["waiting_at"]
        # 배정 요청의 heartbeat가 끊긴 구간을 대기로 추정하지 않는다.
        if owner["waiting"] and 0 <= interval <= 1:
            owner["waiting_seconds"] += interval
        owner.update(waiting=waiting, waiting_at=now)
        if owner["budget_directory"]:
            from loop_manager_internal.verification.timing import atomic_json
            directory = Path(owner["budget_directory"])
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / (session+".json"), {"waiting_seconds": owner["waiting_seconds"]})

    def _allocation(self, session, reply):
        active = any(value["active"] or value["uncertain"] for value in self.reservations.values()
                     if value["session"] == session)
        self._record_wait(session, not reply.get("granted") and not active)
        return reply

    def guard(self, session, callback):
        with self.lock:
            self.sessions[session]["guard"] = callback

    def finish(self, session, *, stopped):
        with self.lock:
            if session in self.sessions:
                self._record_wait(session, False)
            if stopped:
                self.reservations = {key: value for key, value in self.reservations.items() if value["session"] != session}
                self.sessions.pop(session, None)
            else:
                for value in self.reservations.values():
                    if value["session"] == session:
                        value["uncertain"] = True
            if session in self.waiters:
                self.waiters.remove(session)
            self.last_request.pop(session, None)

    def request(self, session, secret, operation, **data):
        with self.lock:
            owner = self.sessions.get(session)
            if owner is None or not secrets.compare_digest(owner["secret"], secret):
                raise RuntimeError("verify 통신 인증 실패")
            if operation == "acquire":
                for key in ("run_id", "unit_id"):
                    value = data.get(key)
                    if value is not None and (not isinstance(value, str) or not value):
                        raise RuntimeError("verify unit identity 형식 오류")
                if owner["require_guard"] and owner["guard"] is None:
                    return {"granted": False, "reason": "verify 모체 소유권 확인 대기"}
                token = data.get("token")
                existing = self.reservations.get(token) if token else None
                if token and (existing is None or existing["session"] != session or existing["active"]):
                    raise RuntimeError("재사용할 idle 예약이 없습니다.")
                snapshot = self.sampler.sample()
                now = self.clock()
                self.last_request[session] = now
                self.waiters = deque(item for item in self.waiters if now - self.last_request.get(item, now) < 1)
                if session not in self.waiters:
                    self.waiters.append(session)
                running = sum(value["active"] for value in self.reservations.values() if value["session"] == session)
                pending = [value for value in self.reservations.values()
                           if value["uncertain"] or value["pid"] is None or
                           value["active"] and now - value["at"] < RESERVATION_GRACE]
                pending_memory = [value for value in self.reservations.values()
                                  if value["uncertain"] or value["pid"] is None or now - value["born_at"] < RESERVATION_GRACE]
                memory = max(DEFAULT_WORKER_BYTES, int(data.get("worker_bytes", DEFAULT_WORKER_BYTES)))
                global_running = sum(value["active"] for value in self.reservations.values())
                diagnostics = {"snapshot": asdict(snapshot), "snapshot_age": now - snapshot.at,
                               "running": running, "global_running": global_running,
                               "pending_cpu": len(pending),
                               "pending_memory": sum(value["bytes"] for value in pending_memory)}
                decision = decide(snapshot, now=now, running=running, cap=owner["cap"],
                                  pending_cpu=diagnostics["pending_cpu"], pending_memory=diagnostics["pending_memory"],
                                  worker_bytes=memory, existing=existing is not None,
                                  global_running=global_running)
                if running >= decision.limit:
                    self.waiters.remove(session)
                    return self._allocation(session, {**asdict(decision), **diagnostics})
                if self.waiters[0] != session:
                    return self._allocation(session, {**asdict(decision), **diagnostics,
                            "granted": False, "reason": "다른 verify의 배정 순서"})
                if decision.granted:
                    self.waiters.popleft()
                else:
                    self.waiters.rotate(-1)
                reply = {**asdict(decision), **diagnostics}
                if decision.granted:
                    token = token or secrets.token_hex(16)
                    if existing:
                        existing.update(active=True, at=now)
                    else:
                        self.reservations[token] = {"session": session, "active": True, "at": now,
                                                    "bytes": memory, "pid": None, "identity": None,
                                                    "uncertain": False, "born_at": now}
                    for key in ("run_id", "unit_id"):
                        value = data.get(key)
                        if value is not None:
                            self.reservations[token][key] = value
                    reply["token"] = token
                return self._allocation(session, reply)
            token = data.get("token")
            self._record_wait(session, False)
            reservation = self.reservations.get(token)
            if reservation is None or reservation["session"] != session:
                raise RuntimeError("다른 verify 또는 알 수 없는 예약입니다.")
            if operation == "bind":
                pid = data.get("pid")
                if not isinstance(pid, int) or pid <= 0 or reservation["pid"] is not None:
                    raise RuntimeError("verify 자식 bind 형식 오류")
                guard = owner["guard"]
                if guard is not None and not guard(pid):
                    raise RuntimeError("선택된 verify의 자식이 아닙니다.")
                identity = self.identity(pid)
                if identity is None:
                    raise RuntimeError("시작된 자식 identity를 확인할 수 없습니다.")
                reservation.update(pid=pid, identity=identity)
            elif operation == "idle":
                if reservation["pid"] is None or reservation["uncertain"]:
                    raise RuntimeError("시작 또는 종료가 불명인 예약입니다.")
                reservation.update(active=False)
            elif operation in {"release", "not_started"}:
                if operation == "not_started":
                    if reservation["pid"] is not None:
                        raise RuntimeError("이미 시작된 자식 예약입니다.")
                elif reservation["pid"] is None or self.identity(reservation["pid"]) == reservation["identity"]:
                    raise RuntimeError("자식 종료를 확인하지 못했습니다.")
                self.reservations.pop(token)
            else:
                raise RuntimeError("허용되지 않은 verify 통신 요청입니다.")
            return {"ok": True}

    def start(self):
        coordinator = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    raw = self.rfile.readline(8193)
                    if len(raw) > 8192 or not raw.endswith(b"\n"):
                        raise RuntimeError("verify 통신 길이/형식 오류")
                    request = json.loads(raw)
                    reply = coordinator.request(**request)
                except Exception as error:
                    reply = {"error": type(error).__name__}
                self.wfile.write(json.dumps(reply).encode() + b"\n")
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="verify-reservations", daemon=True)
        self.thread.start()
        return self

    @property
    def endpoint(self):
        return self.server.server_address

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_error):
        self.close()


class CoordinatorClient:
    def __init__(self, endpoint, session, secret):
        self.endpoint, self.session, self.secret = tuple(endpoint), session, secret

    def request(self, operation, **data):
        value = {"session": self.session, "secret": self.secret, "operation": operation, **data}
        with socket.create_connection(self.endpoint, timeout=3) as connection:
            connection.sendall(json.dumps(value).encode() + b"\n")
            with connection.makefile("rb") as stream:
                raw = stream.readline(8193)
        if len(raw) > 8192 or not raw.endswith(b"\n"):
            raise RuntimeError("verify 조율 응답 형식 오류")
        reply = json.loads(raw)
        if "error" in reply:
            raise RuntimeError(f"verify 조율 실패: {reply['error']}")
        return reply
