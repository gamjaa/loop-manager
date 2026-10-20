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


def progress_observation(directory, positions, state):
    """추가된 진단 행만 읽어 진행량·대기를 설명하며 실행 상태는 변경하지 않는다."""
    for path in directory.rglob("*.jsonl"):
        with path.open(encoding="utf-8") as stream:
            stream.seek(positions.get(path, 0))
            while True:
                line = stream.readline()
                if not line.endswith("\n"):
                    break
                positions[path] = stream.tell()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind, identity = event.get("kind"), event.get("id")
                key = (path, identity)
                if kind == "test_start":
                    state.setdefault("active", {})[key] = identity
                elif kind in {"test_end", "test_interrupted"}:
                    state.setdefault("active", {}).pop(key, None)
                    if kind == "test_end":
                        state["completed"] = state.get("completed", 0) + 1
                elif kind == "allocation" and isinstance(event.get("decision"), dict):
                    decision = event["decision"]
                    state["wait"] = decision.get("reason") if not decision.get("granted") else None
    active = list(state.get("active", {}).values())
    return {"observed_completed_tests": state.get("completed", 0), "active_tests": len(active),
            "current_test": active[0] if active else "-", "resource_wait": state.get("wait") or "-"}


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
        writer = EventWriter(directory / "manager.jsonl")
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
                "verify", describe=lambda: progress_observation(directory, positions, observed)):
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
