"""Shared worker process, IPC, and serial reservation limit."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

from loop_manager_internal.verification.guard import worker_environment


@dataclass
class Worker:
    process: object
    token: str
    number: int
    output: object
    job: object = None
    module: object | None = None
    report: Path | None = None
    log_offset: int = 0
    identity: dict | None = None
    result_observed_at: float | None = None
    result_ready_at: float | None = None
    child_wait_started_at: float | None = None
    child_wait_last_active: int | None = None
    child_wait_seconds: float | None = None
    previous_unit_id: str | None = None
    previous_result_observed_at: float | None = None
    previous_module_result_at: float | None = None

    def assign(self, module, report, directory):
        self.output.flush()
        self.log_offset = self.output.tell()
        self.module, self.report = module, report
        self.process.stdin.write(json.dumps({"module": module.name, "ids": module.ids, "owners": module.owners, "pattern": module.pattern,
                                 "result": str(report), "directory": str(directory), "worker": self.number,
                                 "worker_id": self.number, **(self.identity or {}),
                                 **({"unit_id": module.name} if self.identity else {})}) + "\n")
        self.process.stdin.flush()


def start_worker(token, number, directory, *, command=None, cwd=None, environment=None, identity=None):
    from loop_manager_internal.verification.core import start_process
    command = command or [sys.executable, "-u", "-m", "loop_manager_internal.verification.runner", "--worker"]
    process, output, job = start_process(command, number, directory, cwd=cwd,
        environment=worker_environment(os.environ if environment is None else environment))
    return Worker(process, token, number, output, job, identity=identity)


class SerialClient:
    def __init__(self, parent):
        self.parent = parent
        self.active = False

    def request(self, operation, **data):
        if operation == "acquire" and "token" not in data and self.active:
            return {"granted": False, "reason": "serial stage"}
        reply = self.parent.request(operation, **data)
        if operation == "acquire" and reply["granted"]:
            self.active = True
        return reply
