"""기존 verify 실행 경계에 인스턴스 조율과 상태 디렉터리 진단을 전달한다."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
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
        writer = EventWriter(directory / "manager.jsonl")
        environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory),
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

    The caller must supply the actual effective command/environment and measured
    identities. Unconfirmed Git or external inputs remain unusable for reuse.
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


_COMPILE_RUNTIME_PROBE = r'''
import hashlib, json, os, pathlib, sys
if sys.platform != 'linux':
    raise RuntimeError('native library measurement unavailable')
files = {pathlib.Path(sys.executable).resolve()}
def walk_error(error):
    raise error
for entry in sys.path:
    root = pathlib.Path(entry)
    if root.is_file():
        files.add(root.resolve())
    elif root.is_dir():
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(d for d in dirs if d not in {'site-packages', 'dist-packages'})
            if any((pathlib.Path(directory) / d).is_symlink() for d in dirs):
                raise RuntimeError('external runtime directory')
            files.update(pathlib.Path(directory) / name for name in names)
for line in pathlib.Path('/proc/self/maps').read_text().splitlines():
    parts = line.split(maxsplit=5)
    if len(parts) == 6 and parts[5].startswith('/'):
        files.add(pathlib.Path(parts[5]))
entries = []
for path in sorted(files):
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if any(getattr(before, key) != getattr(after, key) for key in (
        'st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        raise RuntimeError('runtime changed during observation')
    entries.append((str(path), str(path.resolve()), before.st_mode, hashlib.sha256(data).hexdigest()))
value = (sys.version, sys.path, tuple(sys.flags), sys.getfilesystemencoding(),
         os.geteuid(), os.getegid(), os.getgroups(), tuple(os.uname()), entries)
print(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
'''


def observe_isolated_compile(worktree, script, arguments, environment):
    from loop_manager_internal.verification.inputs import InputObservation, digest
    if arguments != isolated_compile_arguments(script, worktree):
        return InputObservation(None, reason='unconfirmed script contract')
    if not (worktree / 'src').is_dir() or (worktree / 'src').is_symlink():
        return InputObservation(None, reason='unconfirmed source directory')
    from loop_manager_internal.verification.contract import load, ContractError
    try:
        connected = load(worktree) is not None
    except ContractError as error:
        return InputObservation(None, reason=f"invalid verify connection: {error}")
    if connected:
        return InputObservation(None, reason='unconfirmed coordinated runtime environment')
    # Native loader hooks would add executable dependencies outside this probe.
    if any(key.startswith(('LD_', 'DYLD_')) for key in environment):
        return InputObservation(None, reason='unconfirmed native loader inputs')
    try:
        # ponytail: Hash the isolated runtime on every observation. Reuse a
        # runtime digest only after an immutable external identity is available.
        result = subprocess.run([arguments[0], '-I', '-S', '-B', '-c', _COMPILE_RUNTIME_PROBE],
            cwd=worktree, env=dict(environment), capture_output=True, text=True,
            timeout=30, check=True)
        identity = result.stdout.strip()
        if len(identity) != 64 or any(char not in '0123456789abcdef' for char in identity):
            raise ValueError('invalid runtime measurement')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return InputObservation(None, reason=str(error))
    # -I -S excludes project imports/site packages; UTF-8 compilation does not
    # execute source code or write bytecode. Its runtime is measured above; src bytes,
    # types and modes still require a complete manifest observation below.
    return observe_inputs(worktree, arguments, environment=environment,
        runtime_identity=digest((_COMPILE_RUNTIME_PROBE, identity)),
        git_inputs={}, external_inputs={'cwd': str(worktree.resolve())})
