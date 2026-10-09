"""도구 독립 자원 배정과 종료 확인. 결과 의미는 adapter가 검증한다."""
import json
import os
import subprocess
import sys
import time
from loop_manager_internal.verification.resources import DEFAULT_WORKER_BYTES
from loop_manager_internal.verification.timing import phase

def start_process(command, number, directory, *, cwd=None, environment=None):
    from loop_manager_internal.execution.verify_process import _LINUX_SUBREAPER
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
                                   text=True, encoding="utf-8", env=environment, cwd=cwd,
                                   **({"start_new_session": True} if os.name != "nt" else {}))
        return process, output, job
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
    print(f"verify 실패 모듈: {worker.module.name}\n{redact_secrets(raw)}", flush=True)


def stop_worker(worker):
    from loop_manager_internal.execution.verify_process import terminate_verify_process
    try:
        worker.process.wait(timeout=10)
        if worker.job:
            worker.job.wait(10)
        if worker.process.returncode != 0:
            raise RuntimeError(f"verify worker 비정상 종료: {worker.process.returncode}")
    except (subprocess.TimeoutExpired, TimeoutError):
        if worker.job:
            errors = []
            for cleanup in (worker.job.terminate, lambda: worker.process.wait(timeout=10), lambda: worker.job.wait(10)):
                try:
                    cleanup()
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError("verify worker 자식 종료 불명: " + "; ".join(errors))
        else:
            terminate_verify_process(worker.process)
        raise RuntimeError("verify worker 종료 시간 초과")
    finally:
        worker.output.close()
        if worker.job:
            worker.job.close()


def execute_manifest(modules, directory, writer, client, *, spawn, validate_report, stop_worker, failure_output, coverage, sleep=time.sleep):
    pending, workers, completed, passed = list(modules), [], set(), True
    budget = DEFAULT_WORKER_BYTES
    last_reason = None
    last_allocation_at = None
    waiting = None
    try:
        while pending or any(worker.module is not None for worker in workers):
            for worker in workers:
                if worker.process.poll() is not None:
                    raise RuntimeError("verify worker가 배정/정리 전에 종료됐습니다.")
                if worker.module is not None and worker.report.is_file():
                    if worker.job and worker.job.active() > 1:
                        continue
                    report = json.loads(worker.report.read_text(encoding="utf-8"))
                    passed = validate_report(worker.module, report) and passed
                    if not report["passed"]:
                        failure_output(worker, directory)
                    if report.get("runner_error"):
                        raise RuntimeError(f"verify worker 수집/실행 오류: {worker.module.name}")
                    covered = coverage(worker.module, report)
                    if completed.intersection(covered):
                        raise RuntimeError("verify 결과 중복")
                    completed.update(covered)
                    memory = report.get("memory")
                    if memory:
                        budget = max(budget, int(memory["peak"] * 1.25))
                    writer.emit("module_result", unit_id=worker.module.name, module=worker.module.name, passed=report["passed"], memory=memory)
                    client.request("idle", token=worker.token)
                    worker.module = None
            if pending:
                idle = next((worker for worker in workers if worker.module is None), None)
                reply = client.request("acquire", **({"token": idle.token} if idle else {}), worker_bytes=budget, run_id=writer.identity.get("run_id"), unit_id=pending[0].name)
                reason = reply["reason"]
                now = writer.clock()
                if reply["granted"] or reason != last_reason or last_allocation_at is None or now - last_allocation_at >= 1:
                    writer.emit("allocation", decision=reply, estimated_worker_bytes=budget, run_id=writer.identity.get("run_id"), unit_id=pending[0].name)
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
                    writer.emit("unit_start", unit_id=module.name, pid=idle.process.pid, reservation=idle.token)
                    idle.assign(module, directory / f"module-{len(modules) - len(pending) - 1}.json", directory)
                elif waiting is None:
                    waiting = writer.emit("phase_start", name="resource_wait")
            if pending or any(worker.module is not None for worker in workers):
                sleep(0.05)
        expected = {identity for module in modules for identity in module.ids}
        if passed and completed != expected:
            raise RuntimeError("verify 최종 결과에 누락이 있습니다.")
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
            raise RuntimeError("verify 자식 정리 실패: " + "; ".join(errors))



def execute_command(argv, cwd, timeout, writer, client, run_id, unit_id, *, run_process=None, sleep=time.sleep):
    """Serial mandatory commands also reserve resources and confirm termination."""
    from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
    from loop_manager_internal.verification.guard import worker_environment
    run_process = run_process or run_verify_process
    started = time.monotonic()
    with phase("resource_wait", writer=writer):
        while True:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError("verify command resource wait timeout")
            reply = client.request("acquire", worker_bytes=DEFAULT_WORKER_BYTES, run_id=run_id, unit_id=unit_id)
            writer.emit("allocation", decision=reply, unit_id=unit_id)
            if reply["granted"]:
                break
            sleep(0.05)
    token = reply["token"]
    launched, stopped, uncertain = False, False, False
    def bind(process, job):
        nonlocal launched
        launched = True
        client.request("bind", token=token, pid=process.pid)
    writer.emit("unit_start", version=1, run_id=run_id, unit_id=unit_id, argv=argv, cwd=str(cwd), kind_of_unit="command")
    try:
        with phase(f"command:{unit_id}", writer=writer):
            passed, timed_out, output = run_process(argv, cwd, None if timeout is None else max(0.001, timeout - (time.monotonic() - started)),
                environment=worker_environment(os.environ), job_scope=True, on_started=bind)
        stopped = True
        from loop_manager_internal.execution.validation_feedback import redact_secrets
        print(redact_secrets(str(output)), end="", flush=True)
        writer.emit("unit_result", version=1, run_id=run_id, unit_id=unit_id, passed=passed,
                    exit_code=getattr(output, "exit_code", None), timed_out=timed_out, kind_of_unit="command")
        return passed
    except VerifyTerminationUncertain:
        uncertain = True
        raise
    finally:
        if stopped:
            client.request("release", token=token)
        elif not launched and not uncertain:
            client.request("not_started", token=token)
