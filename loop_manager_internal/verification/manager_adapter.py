"""기존 verify 실행 경계에 인스턴스 조율과 상태 디렉터리 진단을 전달한다."""
import json
import os
from pathlib import Path
import tempfile

from loop_manager_internal.verification.coordination import VerifyCoordinator, descendant, settings
from loop_manager_internal.verification.timing import EventWriter, atomic_json, diagnostic, phase


def run(effects, arguments, worktree, timeout, *, launch=None):
    context = settings.get()
    coordinator = context.coordinator if context else VerifyCoordinator().start()
    session, secret = coordinator.open(context.cap if context else "auto", require_guard=True)
    base = context.state_dir / "logs/verify" if context else Path(tempfile.gettempdir()) / "loop-manager-verify"
    directory = base / session
    stopped = False
    writer = None
    def started(process, job):
        coordinator.guard(session, job.contains if job else lambda pid: descendant(pid, process.pid))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        writer = EventWriter(directory / "manager.jsonl")
        environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory),
                           PYTHONIOENCODING="utf-8",
                           LOOP_MANAGER_VERIFY_PARALLELISM=str(context.cap if context else "auto"),
                           LOOP_MANAGER_VERIFY_ENDPOINT=json.dumps(coordinator.endpoint),
                           LOOP_MANAGER_VERIFY_SESSION=session, LOOP_MANAGER_VERIFY_SECRET=secret)
        with phase("verify_process", writer=writer):
            passed, timed_out, output = effects.run_verify_process(
                arguments, worktree, timeout, environment=environment, job_scope=True, on_started=started,
                **({"launch_process": launch} if launch else {}),
            )
        stopped = True
        details = diagnostic(directory)
        if not passed:
            from loop_manager_internal.execution.verify_process import VerifyProcessOutput
            output = VerifyProcessOutput(str(output) + "\n" + details + f"\n[VERIFY_DIAGNOSTIC] {directory / 'summary.json'}",
                                         exit_code=getattr(output, "exit_code", None))
        return passed, timed_out, output
    finally:
        try:
            coordinator.finish(session, stopped=stopped)
            if not stopped and directory.is_dir():
                with coordinator.lock:
                    retained = {key: value for key, value in coordinator.reservations.items() if value["session"] == session}
                atomic_json(directory / "uncertain-reservations.json", retained)
                diagnostic(directory)
        finally:
            try:
                if writer:
                    writer.close()
            finally:
                if context is None:
                    coordinator.close()
