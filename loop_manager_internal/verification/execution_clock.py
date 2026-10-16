"""실행별 자원 대기 누계만 제외하는 중첩 프로세스의 공통 시간 기준."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import shutil
import sys
import time
from contextvars import ContextVar


current_execution_clock = ContextVar("verify_execution_clock", default=None)


BUDGET_DIRECTORY = "LOOP_MANAGER_VERIFY_BUDGET_DIR"
_FilesystemPath = type(Path())


class ExecutionClock:
    def __init__(self, environment=None, *, clock=None):
        self.environment = dict(os.environ if environment is None else environment)
        self.temporary = None
        if not self.environment.get(BUDGET_DIRECTORY):
            self.temporary_root = _FilesystemPath(tempfile.gettempdir()).resolve()
            self.temporary = _FilesystemPath(tempfile.mkdtemp(prefix="loop-verify-budget-"))
            self.environment[BUDGET_DIRECTORY] = str(self.temporary)
        self.directory = _FilesystemPath(self.environment[BUDGET_DIRECTORY])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.initial_wait = self.waiting()
        self.last_elapsed = 0.0
        self.local_wait = 0.0
        self.active_processes = 0
        self._tokens = []

    def waiting(self):
        total = 0.0
        for path in self.directory.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))["waiting_seconds"]
                if type(value) in (float, int) and math.isfinite(value) and value >= 0:
                    total += value
            except (OSError, ValueError, KeyError, TypeError):
                # 확인하지 못한 구간은 실제 시간으로 계산한다.
                continue
        return total

    def elapsed(self):
        self.last_elapsed = max(self.last_elapsed, self.clock()-self.started - self.local_wait -
                                max(0, self.waiting()-self.initial_wait))
        return self.last_elapsed

    def remaining(self, timeout):
        return None if timeout is None else max(0.0, timeout-self.elapsed())

    def close(self):
        if self.temporary is not None:
            resolved=self.temporary.resolve()
            if self.temporary.is_symlink() or not resolved.is_relative_to(self.temporary_root):
                raise RuntimeError("실행 시간 기록의 소유 임시 경로가 변경됐습니다.")
            if resolved.exists():
                shutil.rmtree(resolved)

    def require_stopped(self):
        receipt=self.directory / "wsl-lifecycle.json"
        if sys.platform=="win32" and receipt.is_file():
            try:
                stopped=json.loads(receipt.read_text(encoding="utf-8")).get("state")=="stopped"
            except (OSError,ValueError,TypeError):
                stopped=False
            if not stopped:
                from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
                raise VerifyTerminationUncertain(f"WSL 자식 종료 근거를 보존합니다: {receipt}")

    def __enter__(self):
        self._tokens.append(current_execution_clock.set(self))
        return self

    def __exit__(self, _kind, error, _traceback):
        current_execution_clock.reset(self._tokens.pop())
        from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
        if not isinstance(error,VerifyTerminationUncertain):
            self.close()
