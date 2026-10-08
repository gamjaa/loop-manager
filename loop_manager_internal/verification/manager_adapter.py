"""기존 verify 실행 경계에 인스턴스 조율과 상태 디렉터리 진단을 전달한다."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess

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
    if (worktree / 'loop_manager_internal/verification/runner.py').is_file():
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
