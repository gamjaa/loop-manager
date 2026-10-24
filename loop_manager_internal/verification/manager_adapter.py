"""기존 verify 실행 경계에 인스턴스 조율과 상태 디렉터리 진단을 전달한다."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil
import sys
from loop_manager_internal.runtime import operation_events as events

from loop_manager_internal.verification.coordination import VerifyCoordinator, descendant, settings
from loop_manager_internal.verification.timing import EventWriter, atomic_json, diagnostic, phase


def progress_observation(directory, positions, state, run_id=None):
    """추가된 현재 run 이벤트만 읽어 테스트·배정·종료 대기를 연결한다."""
    directory = Path(directory)
    result, snapshot = {}, {}
    read_errors = 0
    for name, target in (("result.json", result), ("snapshot.json", snapshot)):
        path = directory / name
        try:
            if path.is_file() and not path.is_symlink():
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    target.update(value)
        except (OSError, UnicodeError, ValueError):
            read_errors += 1
    if run_id is not None:
        if result.get("run_id") != run_id:
            result = {}
        if snapshot.get("run_id") != run_id:
            snapshot = {}

    sources = [("current", directory)]
    wsl_logs = None
    bundle = snapshot.get("bundle")
    if isinstance(bundle, str):
        candidate = Path(bundle)
        if candidate.name.startswith("loop-manager-verify-") and not candidate.is_symlink():
            live = candidate / "logs"
            if live.is_dir() and not live.is_symlink():
                wsl_logs = live
    if wsl_logs is None:
        wsl_logs = Path(str(directory) + "-wsl")
    if wsl_logs.is_dir() and not wsl_logs.is_symlink():
        sources.append(("wsl", wsl_logs))
    windows_logs = Path(str(directory) + "-windows")
    if windows_logs.is_dir() and not windows_logs.is_symlink():
        sources.append(("windows", windows_logs))
    child_summaries = {}
    for name, root in (("wsl", wsl_logs), ("windows", windows_logs)):
        try:
            summary_path = root / "summary.json"
            value = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else None
            if isinstance(value, dict):
                metadata = value.get("metadata", {})
                if not isinstance(metadata, dict):
                    read_errors += 1
                    continue
                if run_id is None or (
                        name == "wsl" and (metadata.get("parent_run_id") == run_id
                                            or metadata.get("run_id") == result.get("wsl_run_id"))
                        or name == "windows" and (metadata.get("run_id") == run_id
                                                   or metadata.get("parent_run_id") == run_id)):
                    child_summaries[name] = value
        except (OSError, UnicodeError, ValueError):
            read_errors += 1

    active_tests = state.setdefault("active_tests", {})
    active_units = state.setdefault("active_units", {})
    active_phases = state.setdefault("active_phases", {})
    child_waits = state.setdefault("child_waits", {})
    allocation_reasons = state.setdefault("allocation_reasons", {})
    resource_waits = state.setdefault("resource_waits", {})
    seen_results = state.setdefault("seen_results", set())
    ended_runs = state.setdefault("ended_runs", set())
    ended_stages = state.setdefault("ended_stages", set())
    for source, root in sources:
        try:
            paths = sorted(root.rglob("*.jsonl"))
        except OSError:
            read_errors += 1
            continue
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            cursor_key = (source, relative)
            try:
                with path.open(encoding="utf-8") as stream:
                    offset = positions.get(cursor_key, 0)
                    if path.stat().st_size < offset:
                        offset = 0
                    stream.seek(offset)
                    while True:
                        line = stream.readline()
                        if not line.endswith("\n"):
                            break
                        positions[cursor_key] = stream.tell()
                        try:
                            event = json.loads(line)
                        except ValueError:
                            read_errors += 1
                            continue
                        if not isinstance(event, dict):
                            read_errors += 1
                            continue
                        event_run = event.get("run_id")
                        if run_id is not None:
                            if source == "wsl":
                                known_wsl = result.get("wsl_run_id")
                                event_parent = event.get("parent_run_id")
                                linked_parent = event_parent == run_id
                                linked_child = (known_wsl is not None and event_run == known_wsl
                                                and event_parent in (None, run_id))
                                if not (linked_parent or linked_child):
                                    continue
                            elif event_run != run_id and event.get("parent_run_id") != run_id:
                                continue
                        kind = event.get("kind")
                        clock_domain = event.get("clock_domain", "legacy")
                        stage_id = event.get("stage_id")
                        unit_id = event.get("unit_id")
                        worker_id = event.get("worker_id")
                        pid = event.get("pid")
                        context = (source, clock_domain, event_run, event.get("parent_run_id"), stage_id)
                        scope = (*context, worker_id, pid, unit_id)
                        if kind == "run_end":
                            ended_runs.add(context[:4])
                        elif (kind == "phase_end" and isinstance(event.get("name"), str)
                              and event["name"].startswith("stage:")):
                            ended_stages.add(context)
                        scope_ended = context[:4] in ended_runs or context in ended_stages
                        if scope_ended and (
                                kind in {"test_start", "unit_start", "phase_start", "job_child_wait"}
                                or (kind == "allocation" and isinstance(event.get("decision"), dict)
                                    and not event["decision"].get("granted"))):
                            continue
                        if kind == "test_start":
                            test_id = event.get("id")
                            active_tests[(*scope, test_id)] = {
                                "context": context,
                                "run_id": event_run, "clock_domain": clock_domain,
                                "stage_id": stage_id, "unit_id": unit_id,
                                "worker_id": worker_id, "pid": pid, "test_id": test_id,
                            }
                        elif kind in {"test_end", "test_interrupted"}:
                            test_id = event.get("id")
                            active_tests.pop((*scope, test_id), None)
                            if kind == "test_end":
                                result_key = (*scope, test_id, event.get("at"))
                                if result_key not in seen_results:
                                    seen_results.add(result_key)
                                    state["completed"] = state.get("completed", 0) + 1
                        elif kind == "unit_start":
                            key = (*context, worker_id, pid)
                            active_units[key] = {
                                "context": context,
                                "run_id": event_run, "clock_domain": clock_domain,
                                "stage_id": stage_id, "unit_id": unit_id,
                                "worker_id": worker_id, "pid": pid,
                            }
                        elif kind in {"module_result", "unit_result"}:
                            key = (*context, worker_id, event.get("worker_pid", pid))
                            active_units.pop(key, None)
                        elif kind == "phase_start":
                            phase_name = event.get("name")
                            phase_identity = event.get("phase_id")
                            if phase_identity is None:
                                phase_identity = (phase_name, event.get("at"))
                            phase_key = (source, clock_domain, event_run,
                                         event.get("parent_run_id"), pid, "phase", phase_identity)
                            active_phases[phase_key] = {
                                "context": context,
                                "run_id": event_run, "clock_domain": clock_domain,
                                "stage_id": stage_id, "unit_id": unit_id,
                                "phase": phase_name,
                            }
                            if phase_name == "resource_wait":
                                resource_waits[context] = {
                                    "context": context,
                                    "stage_id": stage_id, "unit_id": unit_id,
                                    "reason": allocation_reasons.get(context, "자원 배정 대기"),
                                }
                        elif kind == "phase_end":
                            phase_name = event.get("name")
                            phase_identity = event.get("phase_id")
                            if phase_identity is None:
                                phase_identity = (phase_name, event.get("start"))
                            phase_key = (source, clock_domain, event_run,
                                         event.get("parent_run_id"), pid, "phase", phase_identity)
                            active_phases.pop(phase_key, None)
                            if phase_name == "resource_wait":
                                resource_waits.pop(context, None)
                            elif isinstance(phase_name, str) and phase_name.startswith("stage:"):
                                for collection in (active_tests, active_units, child_waits, resource_waits):
                                    for key, item in list(collection.items()):
                                        if item.get("context") == context:
                                            collection.pop(key, None)
                                for key, item in list(active_phases.items()):
                                    if item.get("context") == context and item.get("phase") != phase_name:
                                        active_phases.pop(key, None)
                        elif kind == "allocation" and isinstance(event.get("decision"), dict):
                            decision = event["decision"]
                            reason = decision.get("reason") if not decision.get("granted") else None
                            allocation_reasons[context] = reason
                            if decision.get("granted"):
                                resource_waits.pop(context, None)
                            else:
                                resource_waits[context] = {
                                    "context": context,
                                    "stage_id": stage_id, "unit_id": unit_id,
                                    "reason": reason or "자원 배정 대기",
                                }
                        elif kind == "job_child_wait":
                            key = (*context, worker_id, unit_id)
                            child_waits[key] = {
                                "context": context, "run_id": event_run,
                                "clock_domain": clock_domain, "stage_id": stage_id,
                                "unit_id": unit_id, "worker_id": worker_id,
                                "active_processes": event.get("active_processes"),
                            }
                        elif kind == "job_child_exit_confirmed":
                            key = (*context, worker_id, unit_id)
                            child_waits.pop(key, None)
                        elif kind == "job_child_exit_unconfirmed":
                            key = (*context, worker_id, unit_id)
                            child_waits[key] = {
                                "context": context, "run_id": event_run,
                                "clock_domain": clock_domain, "stage_id": stage_id,
                                "unit_id": unit_id, "worker_id": worker_id,
                                "status": "미확인",
                            }
                        elif kind == "run_end":
                            for collection in (active_tests, active_units, active_phases,
                                               child_waits, resource_waits):
                                for key, item in list(collection.items()):
                                    item_context = item.get("context")
                                    if (item_context and item_context[:4] == context[:4]) or (
                                            not item_context and len(key) >= 4 and key[:4] == context[:4]):
                                        collection.pop(key, None)
            except (OSError, UnicodeError, ValueError):
                read_errors += 1

    def scope_label(item):
        return "/".join(str(value) for value in
                        (item.get("clock_domain"), item.get("stage_id"), item.get("unit_id"))
                        if value not in (None, "", "legacy")) or "-"
    def scoped_text(item, value):
        scope = scope_label(item)
        return f"{scope}: {value}" if scope != "-" else str(value)

    tests = list(active_tests.values())
    units = list(active_units.values())
    phases = list(active_phases.values())
    waits = list(resource_waits.values())
    child_activity = list(child_waits.values())
    first_test = tests[0] if tests else {}
    first_unit = units[0] if units else {}
    first_phase = phases[0] if phases else {}
    wait_text = ", ".join(scoped_text(item, item.get("reason")) for item in waits) or "-"
    def child_label(item):
        status = item.get("status") or f"active={item.get('active_processes')}"
        return f"{scope_label(item)}: worker={item.get('worker_id')} unit={item.get('unit_id')} {status}"
    child_text = ", ".join(child_label(item) for item in child_activity) or "-"
    phase_text = ", ".join(scoped_text(item, item.get("phase")) for item in phases) or "-"
    def child_status(name, phase_name):
        if result.get(name) not in (None, "NOT_RUN"):
            return result[name]
        child_result = child_summaries.get(name, {}).get("result")
        if isinstance(child_result, dict) and type(child_result.get("passed")) is bool:
            return "PASS" if child_result["passed"] else "FAIL"
        if any(item.get("phase") == phase_name for item in phases):
            return "RUNNING"
        return result.get(name, "-")
    wsl_status = child_status("wsl", "wsl-full")
    windows_status = child_status("windows", "windows-core")
    return {
        "run_id": run_id or result.get("windows_run_id") or "-",
        "observed_completed_tests": state.get("completed", 0),
        "active_tests": len(tests),
        "current_stage": first_test.get("stage_id") or first_unit.get("stage_id") or first_phase.get("stage_id") or "-",
        "current_unit": first_test.get("unit_id") or first_unit.get("unit_id") or "-",
        "current_test": first_test.get("test_id", "-"),
        "active_test_scopes": ", ".join(
            f"{scope_label(item)} test={item.get('test_id')} worker={item.get('worker_id')}"
            for item in tests[:5]) or "-",
        "active_units": len(units),
        "active_phases": phase_text,
        "resource_wait": wait_text,
        "job_child_wait": child_text,
        "windows_wsl": wsl_status,
        "windows_native": windows_status,
        "snapshot_revision": snapshot.get("revision", "-"),
        "progress_observation": "미확인" if read_errors else "확인",
        "progress_read_errors": read_errors,
    }


def run(effects, arguments, worktree, timeout, *, launch=None, partial_base=None):
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
        from loop_manager_internal.verification.execution_clock import current_execution_clock
        budget = current_execution_clock.get()
        budget_directory = budget.directory if budget is not None else directory / "budget"
        coordinator.track_waiting(session, budget_directory)
        writer = EventWriter(directory / "manager.jsonl", identity={"run_id": session})
        environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory),
                           LOOP_MANAGER_VERIFY_BUDGET_DIR=str(budget_directory),
                           LOOP_MANAGER_VERIFY_RUNNER=str(Path(__file__).with_name("entrypoint.py").resolve()),
                           LOOP_MANAGER_VERIFY_RUNTIME_PYTHON=sys.executable,
                           LOOP_MANAGER_VERIFY_VERSION="1",
                           LOOP_MANAGER_VERIFY_TIMEOUT=str(timeout),
                           LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(Path(worktree).resolve()),
                           PYTHONIOENCODING="utf-8",
                           LOOP_MANAGER_VERIFY_PARALLELISM=str(context.cap if context else "auto"),
                           LOOP_MANAGER_VERIFY_ENDPOINT=json.dumps(coordinator.endpoint),
                           LOOP_MANAGER_VERIFY_SESSION=session, LOOP_MANAGER_VERIFY_SECRET=secret)
        environment.pop("LOOP_MANAGER_VERIFY_PARTIAL_BASE", None)
        if partial_base:
            environment["LOOP_MANAGER_VERIFY_PARTIAL_BASE"] = partial_base
        positions, observed = {}, {}
        with phase("verify_process", writer=writer), events.activity(
                "부분 verify 실행" if partial_base else "전체 verify 실행", run_id=session,
                diagnostic_path=directory / "summary.json") as report, events.progress(
                "verify", describe=lambda: progress_observation(directory, positions, observed, session)):
            passed, timed_out, output = effects.run_verify_process(
                arguments, worktree, timeout, environment=environment, job_scope=True, on_started=started,
                **({"launch_process": launch} if launch else {}),
            )
            report.update(result="TIMEOUT" if timed_out else "프로세스 정상 종료" if passed else "FAIL",
                          level="INFO" if passed else "WARN", exit_code=getattr(output, "exit_code", None))
        stopped = True
        details = diagnostic(directory)
        events.emit("verify 상세 진단 저장", diagnostic_path=directory / "summary.json")
        partial_report = None
        if partial_base:
            from loop_manager_internal.execution.verify_process import VerifyProcessOutput
            output = VerifyProcessOutput(str(output) + "\n" + details, exit_code=getattr(output, "exit_code", None))
            report = directory / "partial-result.json"
            try:
                partial_report = json.loads(report.read_text(encoding="utf-8"))
                if (not isinstance(partial_report, dict) or partial_report.get('run_id') != session
                        or type(partial_report.get('version')) is not int or partial_report.get('version') != 1
                        or partial_report.get('kind') != 'partial_verify'):
                    raise ValueError('partial report session/version mismatch')
            except (OSError, ValueError):
                partial_report = {"kind": "partial_verify", "status": "ERROR", "diagnostic": str(directory)}
            output.partial_result = partial_report
        if not passed:
            from loop_manager_internal.execution.verify_process import VerifyProcessOutput
            output = VerifyProcessOutput(str(output) + "\n" + details + f"\n[VERIFY_DIAGNOSTIC] {directory / 'summary.json'}",
                                         exit_code=getattr(output, "exit_code", None))
            if partial_base:
                output.partial_result = partial_report
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


def observe_inputs(worktree, arguments, *, environment, runtime_identity,
                   git_inputs=None, external_inputs=None, completeness=None):
    """Provide candidate observations independently of execution/request creation.

    The command identifies the selected repository execution path. Environment,
    runtime, Git and external identities do not gate repository-code reuse.
    """
    from loop_manager_internal.verification.inputs import observe
    return observe(Path(worktree), command=tuple(arguments), environment=environment,
                   runtime=runtime_identity, git_inputs=git_inputs,
                   external_inputs=external_inputs, completeness=completeness)


# A bounded, optional adapter, not an analysis of arbitrary shell programs.
# Exact bytes opt into a stdlib-only operation with no project code execution.
_ISOLATED_COMPILE_PROGRAM = (
    'from pathlib import Path; root = Path("src"); assert root.is_dir(); '
    '[compile(path.read_text(encoding="utf-8"), str(path), "exec") '
    'for path in root.rglob("*.py") '
    'if not {".git", ".venv", "__pycache__"}.intersection(path.relative_to(root).parts)]'
)
ISOLATED_COMPILE_SCRIPT = (
    '#!/usr/bin/env bash\n# loop-manager: isolated-compile-v1\nset -eu\n'
    f"exec python3 -I -S -B -c '{_ISOLATED_COMPILE_PROGRAM}'\n"
).encode()


def isolated_compile_arguments(script, worktree):
    if os.name != 'posix':
        return None
    try:
        if script.is_symlink() or script.read_bytes() != ISOLATED_COMPILE_SCRIPT:
            return None
    except OSError:
        return None
    executable = shutil.which('python3')
    if executable is None:
        return None
    return [str(Path(executable).resolve()), '-I', '-S', '-B', '-c',
            _ISOLATED_COMPILE_PROGRAM]


def reconfirm_documents(worktree, observation, connection, script):
    """Renew bounded document exclusions from current code and current bytes.

    Unsupported launchers, native consumers or uncertain Python reads retain the
    full-run fallback. A passing run is never evidence for an exclusion.
    """
    if (observation.reason != 'unconfirmed inputs outside manifest'
            or connection is None or connection.adapter != 'unittest'):
        return observation
    from loop_manager_internal.verification import impact_analysis
    from loop_manager_internal.verification.inputs import observe
    try:
        # ponytail: only the shipped launcher has an audited no-document contract.
        # Extend with explicit launcher contracts; do not infer arbitrary shell IO.
        launcher = '35a1b9a32849f1da31b25c0ecb780a2599664394e095f56fbfce535342bd2578'
        if script.name != 'verify.sh' or hashlib.sha256(script.read_bytes()).hexdigest() != launcher:
            return replace(observation, reason='unconfirmed document launcher contract')
        selected = {item[0]: item for item in observation.files}
        if selected[script.relative_to(worktree).as_posix()][3] != launcher:
            return replace(observation, reason='document launcher changed during observation')
        documents = {}
        # Reject unsupported roles before parsing the project. These preliminary
        # checks never authorize reuse; the code consumption proof follows below.
        for path, kind, mode, expected in observation.uncovered:
            if kind != 'file':
                return observation
            content = (worktree / path).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                return replace(observation, reason='document changed during exclusion review')
            # The shared proof consumes Git modes; the final observation retains
            # actual permissions, including a restrictive process umask.
            versions = {label: ('100755' if mode & 0o111 else '100644', content)
                        for label in ('base', 'head')}
            proof, reason = impact_analysis.review_document(path, ({}, {}),
                {'stages': connection.stages}, {path: versions}, (), {})
            if proof is None:
                return replace(observation, reason=reason)
            documents[path] = versions
        facts = {}
        for path, kind, _mode, expected in observation.files:
            if not path.endswith('.py') or kind != 'file':
                continue
            content = (worktree / path).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                return replace(observation, reason='code changed during exclusion review')
            facts[path] = impact_analysis.analyze(path, content)
        graph = impact_analysis.resolve(facts)
        # Unknown libraries/native APIs may consume repository documents too.
        # Only bounded project references and these stdlib contracts are audited.
        local = {path.split('/')[0].removesuffix('.py') for path in facts}
        safe = {'unittest', 'pathlib', 'json', 'math', 'typing', 'dataclasses',
                'collections', 'functools', 'hashlib', 're', 'enum'}
        if any(name.split('.')[0] not in local | safe
               for item in facts.values() for declaration in item['imports']
               for name in ([declaration['module']] if declaration['module'] else declaration['names'])
               if not declaration['level']):
            return replace(observation, reason='unconfirmed document library consumption')
        if not graph or any(item.get('selection_uncertainties') for item in graph.values()):
            return replace(observation, reason='unconfirmed document code consumption')
        inventory = {path: {'base': '100755' if mode & 0o111 else '100644',
                            'head': '100755' if mode & 0o111 else '100644'}
                     for path, kind, mode, _sha in (*observation.files, *observation.uncovered)
                     if kind == 'file'}
        for path in documents:
            proof, reason = impact_analysis.review_document(path, (graph, graph),
                {'stages': connection.stages}, documents, tuple(graph), inventory)
            if proof is None:
                return replace(observation, reason=reason)
        # Re-read all code/doc inputs and bind the proof to this exact observation.
        return observe(worktree, command=(script.relative_to(worktree).as_posix(),),
            environment={}, runtime=None, completeness=observation.completeness_token)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        return replace(observation, reason='document exclusion observation failed: ' + str(error))
