"""같은 manifest를 직렬/부하 적응형 worker 프로세스에서 실행한다."""
from __future__ import annotations
import argparse
from dataclasses import asdict, dataclass
import json
import os
import platform
from pathlib import Path
import py_compile
import subprocess
import sys
import time
import traceback
import unittest
import uuid

from loop_manager_internal.verification.coordination import CoordinatorClient, VerifyCoordinator
from loop_manager_internal.verification.guard import MARKER, install_guard, worker_environment
from loop_manager_internal.verification.manifest import Module, discover, load_module, test_cases, validate_report
from loop_manager_internal.verification.resources import DEFAULT_WORKER_BYTES, ResourceSampler, parse_parallelism, process_memory
from loop_manager_internal.verification.timing import EventWriter, TimedResult, atomic_json, comparison, diagnostic, fixture_timing, phase, summarize


def worker_main():
    os.environ[MARKER] = "1"
    install_guard()
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "worker PR_SET_CHILD_SUBREAPER")
    sys.path.insert(0, str(Path.cwd() / "tests"))
    writer = None
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if writer is None:
                writer = EventWriter(Path(request["directory"]) / f"worker-{request['worker']}.jsonl")
            module = Module(request["module"], tuple(request["ids"]), tuple(tuple(owner) for owner in request["owners"]), request["pattern"])
            try:
                with writer.activate(), phase(f"import:{module.name}"):
                    suite = load_module(module)
                    owners = {test.id(): (test.__class__.__module__,
                              f"{test.__class__.__module__}.{test.__class__.__qualname__}") for test in test_cases(suite)}
                with writer.activate(), phase(f"module:{module.name}"), fixture_timing():
                    result = unittest.TextTestRunner(
                        resultclass=lambda *args, **kwargs: TimedResult(*args, writer=writer, **kwargs),
                        stream=sys.stderr,
                    ).run(suite)
                if sys.platform.startswith("linux"):
                    with writer.activate(), phase(f"descendant_cleanup:{module.name}"):
                        while True:
                            try:
                                os.waitpid(-1, 0)
                            except ChildProcessError:
                                break
                report = {"module": module.name, "passed": result.wasSuccessful(),
                          "completed": result.completed, "memory": process_memory()}
                fixture_skips = {test.id() for test, _reason in result.skipped if test.id() not in result.completed}
                report["fixture_skip_sources"] = sorted(fixture_skips)
                report["fixture_skipped"] = [identity for identity, (owner, klass) in owners.items()
                    if identity not in result.completed and
                    (f"setUpModule ({owner})" in fixture_skips or f"setUpClass ({klass})" in fixture_skips)]
            except Exception:
                traceback.print_exc()
                report = {"module": module.name, "passed": False, "completed": [], "runner_error": True}
            sys.stdout.flush()
            sys.stderr.flush()
            atomic_json(Path(request["result"]), report)
    finally:
        if writer:
            writer.close()
    return 0


@dataclass
class Worker:
    process: object
    token: str
    number: int
    output: object
    job: object = None
    module: Module | None = None
    report: Path | None = None
    log_offset: int = 0

    def assign(self, module, report, directory):
        self.output.flush()
        self.log_offset = self.output.tell()
        self.module, self.report = module, report
        self.process.stdin.write(json.dumps({"module": module.name, "ids": module.ids, "owners": module.owners, "pattern": module.pattern,
                                 "result": str(report), "directory": str(directory), "worker": self.number}) + "\n")
        self.process.stdin.flush()


def start_worker(token, number, directory):
    from loop_manager_internal.execution.verify_process import _LINUX_SUBREAPER
    command = [sys.executable, "-u", "-m", "loop_manager_internal.verification.runner", "--worker"]
    if sys.platform.startswith("linux"):
        # 재사용 worker의 모듈 입력 pipe는 subreaper를 통과해 실제 worker에 전달한다.
        bootstrap = _LINUX_SUBREAPER.replace("stdin=subprocess.DEVNULL", "stdin=sys.stdin")
        command = [sys.executable, "-c", bootstrap, *command]
    output = (directory / f"worker-{number}.log").open("wb")
    process = None
    job = None
    try:
        if os.name == "nt":
            from loop_manager_internal.execution.windows_job import WindowsJob
            job = WindowsJob()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", env=worker_environment(os.environ),
                                   **({"start_new_session": True} if os.name != "nt" else {}))
        return Worker(process, token, number, output, job)
    except BaseException:
        if process is not None:
            process.kill()
            process.wait(timeout=10)
        if job:
            job.close()
        output.close()
        raise


def failure_output(worker, directory):
    """재사용 worker 로그에서 이번 실패 모듈의 원문만 전달한다."""
    from loop_manager_internal.execution.validation_feedback import redact_secrets
    path = directory / f"worker-{worker.number}.log"
    with path.open("rb") as stream:
        stream.seek(worker.log_offset)
        raw = stream.read().decode("utf-8", errors="replace")
    print(f"unittest 실패 모듈: {worker.module.name}\n{redact_secrets(raw)}", flush=True)


def stop_worker(worker):
    from loop_manager_internal.execution.verify_process import terminate_verify_process
    try:
        worker.process.wait(timeout=10)
        if worker.job:
            worker.job.wait(10)
        if worker.process.returncode != 0:
            raise RuntimeError(f"unittest worker 비정상 종료: {worker.process.returncode}")
    except (subprocess.TimeoutExpired, TimeoutError):
        if worker.job:
            errors = []
            for cleanup in (worker.job.terminate, lambda: worker.process.wait(timeout=10), lambda: worker.job.wait(10)):
                try:
                    cleanup()
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError("unittest worker 자식 종료 불명: " + "; ".join(errors))
        else:
            terminate_verify_process(worker.process)
        raise RuntimeError("unittest worker 종료 시간 초과")
    finally:
        worker.output.close()
        if worker.job:
            worker.job.close()


def execute_manifest(modules, directory, writer, client, *, spawn=start_worker, sleep=time.sleep):
    pending, workers, completed, passed = list(modules), [], set(), True
    budget = DEFAULT_WORKER_BYTES
    last_reason = None
    last_allocation_at = None
    waiting = None
    try:
        while pending or any(worker.module is not None for worker in workers):
            for worker in workers:
                if worker.process.poll() is not None:
                    raise RuntimeError("unittest worker가 배정/정리 전에 종료됐습니다.")
                if worker.module is not None and worker.report.is_file():
                    if worker.job and worker.job.active() > 1:
                        continue
                    report = json.loads(worker.report.read_text(encoding="utf-8"))
                    passed = validate_report(worker.module, report) and passed
                    if not report["passed"]:
                        failure_output(worker, directory)
                    if report.get("runner_error"):
                        raise RuntimeError(f"unittest worker 수집/실행 오류: {worker.module.name}")
                    covered = report["completed"] + report.get("fixture_skipped", [])
                    if completed.intersection(covered):
                        raise RuntimeError("unittest 결과 중복")
                    completed.update(covered)
                    memory = report.get("memory")
                    if memory:
                        budget = max(budget, int(memory["peak"] * 1.25))
                    writer.emit("module_result", module=worker.module.name, passed=report["passed"], memory=memory)
                    client.request("idle", token=worker.token)
                    worker.module = None
            if pending:
                idle = next((worker for worker in workers if worker.module is None), None)
                reply = client.request("acquire", **({"token": idle.token} if idle else {}), worker_bytes=budget)
                reason = reply["reason"]
                now = writer.clock()
                if reply["granted"] or reason != last_reason or last_allocation_at is None or now - last_allocation_at >= 1:
                    writer.emit("allocation", decision=reply, estimated_worker_bytes=budget)
                    last_reason = reason
                    last_allocation_at = now
                if reply["granted"]:
                    if waiting is not None:
                        writer.emit("phase_end", name="resource_wait", start=waiting["at"],
                                    duration=writer.clock() - waiting["at"], result="complete")
                        waiting = None
                    if idle is None:
                        try:
                            idle = spawn(reply["token"], len(workers), directory)
                        except BaseException:
                            client.request("not_started", token=reply["token"])
                            raise
                        workers.append(idle)
                        client.request("bind", token=idle.token, pid=idle.process.pid)
                        if idle.job:
                            idle.job.assign(idle.process)
                    module = pending.pop(0)
                    idle.assign(module, directory / f"module-{len(modules) - len(pending) - 1}.json", directory)
                elif waiting is None:
                    waiting = writer.emit("phase_start", name="resource_wait")
            if pending or any(worker.module is not None for worker in workers):
                sleep(0.05)
        expected = {identity for module in modules for identity in module.ids}
        if passed and completed != expected:
            raise RuntimeError("unittest 최종 결과에 누락이 있습니다.")
        return passed
    finally:
        errors = []
        for worker in workers:
            try:
                worker.process.stdin.close()
            except Exception as error:
                errors.append(str(error))
        for worker in workers:
            try:
                with phase(f"worker_shutdown:{worker.number}", writer=writer):
                    stop_worker(worker)
                client.request("release", token=worker.token)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("unittest 자식 정리 실패: " + "; ".join(errors))


def compile_project(root):
    for path in [root / "loop_manager.py", *sorted((root / "loop_manager_internal").rglob("*.py"))]:
        py_compile.compile(str(path), doraise=True)


def run(directory, cap):
    directory.mkdir(parents=True, exist_ok=True)
    os.environ[MARKER] = "1"
    install_guard()
    writer = EventWriter(directory / "runner.jsonl")
    coordinator = None
    session = None
    try:
        if "LOOP_MANAGER_VERIFY_ENDPOINT" in os.environ:
            client = CoordinatorClient(json.loads(os.environ["LOOP_MANAGER_VERIFY_ENDPOINT"]),
                                       os.environ["LOOP_MANAGER_VERIFY_SESSION"], os.environ["LOOP_MANAGER_VERIFY_SECRET"])
            coordinated = True
        else:
            coordinator = VerifyCoordinator()
            session, secret = coordinator.open(cap)
            class LocalClient:
                def request(self, operation, **data):
                    return coordinator.request(session, secret, operation, **data)
            client, coordinated = LocalClient(), False
        def git_value(*arguments):
            return subprocess.run(["git", *arguments], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        writer.emit("run_start", revision=git_value("rev-parse", "HEAD"), git=git_value("--version"),
                    python=sys.version, command=sys.argv, cap=cap, coordinated=coordinated,
                    environment={"host": platform.node(), "platform": sys.platform,
                                 "limits": asdict(ResourceSampler().sample())},
                    estimate={"worker_bytes": DEFAULT_WORKER_BYTES, "measured": False})
        with writer.activate(), phase("verify"):
            with phase("discovery/import"):
                modules = discover(Path.cwd() / "tests")
                atomic_json(directory / "manifest.json", [asdict(module) for module in modules])
            passed = execute_manifest(modules, directory, writer, client)
            if passed:
                with phase("compile"):
                    compile_project(Path.cwd())
            writer.emit("run_end", passed=passed, memory=process_memory())
            return 0 if passed else 1
    finally:
        writer.close()
        if coordinator and session:
            coordinator.finish(session, stopped=not coordinator.reservations)
        print(diagnostic(directory), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--report", nargs="+", type=Path)
    parser.add_argument("--parallelism", default=os.environ.get("LOOP_MANAGER_VERIFY_PARALLELISM", "auto"))
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        return worker_main()
    if arguments.report:
        summaries = [summarize(path) for path in arguments.report]
        print(json.dumps({"summaries": summaries, "comparison": comparison(arguments.report, summaries)},
                         ensure_ascii=False, indent=2))
        return 0
    cap = parse_parallelism(arguments.parallelism)
    directory = Path(os.environ.get("LOOP_MANAGER_VERIFY_LOG_DIR", str(Path.cwd() / ".cache/verify" / uuid.uuid4().hex)))
    if arguments.execute or "LOOP_MANAGER_VERIFY_SESSION" in os.environ:
        return run(directory, cap)
    # 단독 스크립트도 기존 전체 verify timeout과 자식 종료 확인 경계를 사용한다.
    from loop_manager_internal.execution.verify_process import run_verify_process
    environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory), PYTHONIOENCODING="utf-8")
    timeout = 600
    config = Path("config/config.ini")
    if config.is_file():
        from loop_manager_internal.config.managed_config import load_managed_config
        managed = load_managed_config(config)
        timeout = managed.verify_timeout_seconds
        if "--parallelism" not in sys.argv and "LOOP_MANAGER_VERIFY_PARALLELISM" not in os.environ:
            cap = managed.verify_parallelism
    passed, timed_out, output = run_verify_process(
        [sys.executable, "-u", "-m", "loop_manager_internal.verification.runner", "--execute", "--parallelism", str(cap)],
        Path.cwd(), timeout, environment=environment, job_scope=True)
    print(output, end="")
    if timed_out:
        print(diagnostic(directory))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
