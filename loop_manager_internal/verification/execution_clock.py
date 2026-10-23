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


class VerifyBudgetError(RuntimeError):
    """대기 근거가 불일치하므로 원본 누계를 보존하고 성공을 거부한다."""

    diagnostic_directory = None


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
        self.wait_records = {}
        self.wait_error = None
        self.initial_wait = self.waiting()
        self.last_elapsed = 0.0
        self.local_wait = 0.0
        self.active_processes = 0
        self._tokens = []

    def waiting(self):
        if self.wait_error is not None:
            raise self.wait_error
        records = {}
        try:
            paths = list(self.directory.iterdir())
        except OSError:
            self._reject_wait(f"실행 대기 누계 디렉터리를 읽지 못했습니다: {self.directory}")
        for path in paths:
            if path.suffix != ".json" or path.name == "wsl-lifecycle.json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))["waiting_seconds"]
                if (type(value) not in (float, int) or not math.isfinite(value)
                        or value < self.wait_records.get(path.name, 0)):
                    raise ValueError("invalid or regressing wait total")
                records[path.name] = value
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                self._reject_wait(f"실행 대기 누계가 손상되거나 역행했습니다: {path}")
        if self.wait_records.keys() - records.keys():
            self._reject_wait(f"실행 대기 누계가 사라졌습니다: {self.directory}")
        try:
            total = math.fsum(records.values())
        except OverflowError:
            self._reject_wait(f"실행 대기 누계가 범위를 초과했습니다: {self.directory}")
        self.wait_records = records
        return total

    def _reject_wait(self, message):
        evidence = None
        try:
            evidence = _FilesystemPath(tempfile.mkdtemp(prefix="error-", dir=self.directory))
            for source in self.directory.glob("*.json"):
                shutil.copy2(source, evidence / source.name, follow_symlinks=False)
            (evidence / "observed-records").write_text(json.dumps(self.wait_records), encoding="utf-8")
            message += f"; 원본 진단: {evidence}"
        except OSError as error:
            message += f"; 원본 진단 복사 실패: {error}"
        self.wait_error = VerifyBudgetError(message)
        self.wait_error.diagnostic_directory = evidence
        raise self.wait_error from None

    def elapsed(self):
        self.last_elapsed = max(self.last_elapsed, self.clock()-self.started - self.local_wait -
                                max(0, self.waiting()-self.initial_wait))
        return self.last_elapsed

    def remaining(self, timeout):
        elapsed = self.elapsed()
        return None if timeout is None else max(0.0, timeout-elapsed)

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
        if not isinstance(error,(VerifyTerminationUncertain, VerifyBudgetError)):
            self.close()
