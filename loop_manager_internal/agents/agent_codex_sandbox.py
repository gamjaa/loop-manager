"""Direct Codex launch with a separate native tool-policy preflight."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import tempfile
import threading

CODEX_EXTENSION_ARGUMENTS = ('-c', 'features.plugins=false', '-c', 'features.apps=false')


@dataclass(frozen=True)
class CodexLaunch:
    arguments: tuple[str, ...]
    probe_arguments: tuple[str, ...]
    environment: dict[str, str]
    runtime: Path
    attempt: Path
    specification: str

    @staticmethod
    def stop(process) -> None:
        if os.name == 'nt':
            from .windows_runtime import stop
            stop(process)
            return
        lock = vars(process).get('_agent_stop_lock')
        if lock is not None:
            with lock:
                if vars(process).get('_agent_stop_error') is not None:
                    raise process._agent_stop_error
                if vars(process).get('_agent_stop_confirmed'):
                    return
                try:
                    CodexLaunch._stop_posix(process)
                except BaseException as error:
                    process._agent_stop_error = error
                    raise
                process._agent_stop_confirmed = True
            return
        CodexLaunch._stop_posix(process)

    @staticmethod
    def _stop_posix(process) -> None:
        if sys.platform.startswith('linux') and process.poll() is None:
            from loop_manager_internal.execution.verify_process import terminate_verify_process
            terminate_verify_process(process)
            return
        if (sys.platform.startswith('linux') and isinstance(process.returncode, int)
                and process.returncode < 0):
            from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
            raise VerifyTerminationUncertain('Agent supervisor 종료 후 자식 종료 근거가 없습니다.')
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        from loop_manager_internal.execution.verify_process import _confirm_posix_group_stopped
        _confirm_posix_group_stopped(process.pid)

    @staticmethod
    def start(arguments, *, launch_process, **kwargs):
        if os.name == 'nt':
            from .windows_runtime import start
            return start(arguments, launch_process, **kwargs)
        if sys.platform.startswith('linux'):
            from loop_manager_internal.execution.verify_process import _LINUX_SUBREAPER
            arguments = [sys.executable, '-c',
                         _LINUX_SUBREAPER.replace('stdin=subprocess.DEVNULL', 'stdin=None'), *arguments]
        process = launch_process(arguments, stdin=subprocess.PIPE, start_new_session=True, **kwargs)
        if sys.platform.startswith('linux'):
            process._agent_stop_lock = threading.Lock()
        return process

    def run(self, arguments: list[str], *,
            launch_process: Callable[..., subprocess.Popen],
            on_process_started: Callable[[], None] | None = None,
            on_process_finished: Callable[[], None] | None = None,
            **kwargs):
        prompt = kwargs.pop('input')
        timeout = kwargs.pop('timeout')
        # The caller owns the atomic runtime handoff check at process creation.
        process = self.start(arguments, launch_process=launch_process, **kwargs)
        try:
            from . import process_receipt
            if on_process_started is not None:
                on_process_started()
            process_receipt.started(self, process)
            process.communicate(prompt, timeout=timeout)
            return subprocess.CompletedProcess(arguments, process.returncode)
        finally:
            # Direct exec can still spawn tools. Reap the whole process group.
            try:
                self.stop(process)
            finally:
                process.wait(timeout=10)
            process_receipt.finished(self, process)
            if on_process_finished is not None:
                on_process_finished()

    def probe(self, *, on_features_checked: Callable[[], None] | None = None) -> None:
        feature_result = subprocess.run(
            ('codex', *CODEX_EXTENSION_ARGUMENTS, 'features', 'list'),
            cwd=json.loads(self.specification)['worktree'], env=self.environment,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=False)
        feature_states = {}
        for line in feature_result.stdout.splitlines():
            fields = line.split()
            if fields and fields[0] in ('plugins', 'apps'):
                if fields[0] in feature_states or len(fields) < 3:
                    raise RuntimeError('Codex 확장 도구 비활성화 결과를 확인할 수 없습니다.')
                feature_states[fields[0]] = fields[-1]
        if feature_result.returncode or feature_states != {'plugins': 'false', 'apps': 'false'}:
            raise RuntimeError('Codex 확장 도구 비활성화 결과를 확인할 수 없습니다.')
        if on_features_checked is not None:
            on_features_checked()
        result = subprocess.run(self.probe_arguments, cwd=json.loads(self.specification)['worktree'],
                                env=self.environment,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=False)
        if result.returncode or result.stdout.strip() != 'agent-tool-boundary-v2':
            raise RuntimeError('Codex 내장 도구 sandbox의 파일 쓰기 경계를 확인할 수 없습니다.')


def _owned_directory(path: Path) -> None:
    if os.name == 'nt' and sys.version_info < (3, 13):
        raise RuntimeError('Windows의 비공개 runtime 디렉터리 생성에는 Python 3.13 이상이 필요합니다.')
    path.mkdir(mode=0o700, exist_ok=True)
    status = path.lstat()
    if (not stat.S_ISDIR(status.st_mode) or path.resolve() != path.absolute()
            or path.is_symlink() or getattr(path, 'is_junction', lambda: False)()):
        raise RuntimeError('Codex runtime 경로의 소유권·권한을 확인할 수 없습니다.')
    if os.name == 'nt':
        from .windows_runtime import assert_private_directory
        assert_private_directory(path)
    elif status.st_uid != os.getuid() or status.st_mode & 0o077:
        raise RuntimeError('Codex runtime 경로의 소유권·권한을 확인할 수 없습니다.')


def prepare_launch(boundary, state_dir: Path, role: str, read_only: bool,
                   arguments: tuple[str, ...], environment: dict[str, str]) -> CodexLaunch:
    # 이전 역할별 홈은 보존하고 runtime은 호출별 임시 저장에만 사용한다.
    base = state_dir.resolve() / 'agent-runtime'
    user_home = Path(environment.get('HOME', environment.get('USERPROFILE', str(Path.home()))))
    source_path = Path(environment.get('CODEX_HOME', str(user_home / '.codex'))).absolute()
    source_home = source_path.resolve()
    # Validate containment before mkdir/link/mkdtemp: a misregistered state root
    # must not even create a runtime directory in the user's source checkout.
    for denied in (boundary.source, *boundary.other_worktrees, boundary.worktree,
                   boundary.common_dir, boundary.git_dir, source_home):
        resolved = denied.resolve()
        if base == resolved or resolved in base.parents or base in resolved.parents:
            raise RuntimeError('Codex runtime과 보호 경로가 겹칩니다.')
    from .provider_home import prepare_authentication
    if source_path == state_dir.resolve() / 'automation-codex':
        prepare_authentication('codex', environment)
    identity = sha256((boundary.repository_identity + '\0' + str(boundary.worktree)
                       + '\0' + role).encode()).hexdigest()
    _owned_directory(base)
    runtime = base / identity
    _owned_directory(runtime)
    attempt = Path(tempfile.mkdtemp(prefix='call-', dir=runtime))
    child_environment = dict(environment)
    child_environment['CODEX_HOME'] = str(source_home)
    child_environment['TMPDIR'] = str(attempt)
    if role == 'worker' and not read_only:
        child_environment['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'] = str(attempt)
        child_environment['LOOP_MANAGER_TARGETED_TEST_RUNNER'] = str(
            Path(__file__).resolve().parents[1] / 'verification' / 'targeted.py')
    if os.name == 'nt':
        child_environment['TEMP'] = child_environment['TMP'] = str(attempt)
    writable = [] if read_only else [attempt]
    if not read_only:
        writable.append(boundary.worktree)
    protected = [boundary.source, *boundary.other_worktrees, boundary.common_dir,
                 boundary.git_dir, boundary.worktree / '.git', source_home,
                 *((source_home / name).resolve() for name in ('auth.json', 'config.toml')
                   if (source_home / name).exists())]
    if read_only:
        protected.append(boundary.worktree)
    # Roots may not include protected storage even through an alias.
    for root in writable:
        for denied in protected:
            resolved = denied.resolve()
            if (root == resolved or resolved in root.parents or
                    (root in resolved.parents and root != boundary.worktree)):
                raise RuntimeError('Codex runtime과 보호 경로가 겹칩니다.')
    files = [boundary.common_dir / 'HEAD', boundary.git_dir / 'HEAD']
    if (boundary.worktree / '.git').is_file():
        files.append(boundary.worktree / '.git')
    # Probe the same tool boundary in one standalone sandbox. Never wrap exec.
    # Mirror workspace-write's built-in protected paths in this explicit profile.
    for root in writable:
        for name in ('.git', '.codex', '.agents'):
            target = root / name
            if target.exists():
                protected.append(target)
    specification = json.dumps({'writable': [str(path) for path in writable],
                                'protected': [str(path) for path in protected],
                                'files': [str(path) for path in files],
                                'worktree': str(boundary.worktree)}, sort_keys=True)
    permissions = {":root": "read", **{str(path): "write" for path in writable},
                   **{str(path): "read" for path in protected}}
    filesystem = "{" + ",".join(
        json.dumps(path) + "=" + json.dumps(access)
        for path, access in permissions.items()
    ) + "}"
    probe_arguments = ('codex', 'sandbox', '-C', str(boundary.worktree),
              *CODEX_EXTENSION_ARGUMENTS,
              '--permission-profile', 'loop-manager-tools', '--include-managed-config',
              '-c', 'permissions.loop-manager-tools.filesystem=' + filesystem,
              '-c', 'permissions.loop-manager-tools.network.enabled=false',
              '--', sys.executable, '-I', str(Path(__file__).with_name('agent_sandbox_exec.py')),
              specification)
    if not read_only:
        arguments = (*arguments, '-c',
                     'sandbox_workspace_write.writable_roots=' + json.dumps([str(attempt)]))
    if os.name == 'nt':
        arguments = (*arguments, '-c', 'windows.sandbox="elevated"')
        probe_arguments = (*probe_arguments[:1], '-c', 'windows.sandbox="elevated"', *probe_arguments[1:])
    return CodexLaunch(arguments, probe_arguments, child_environment, runtime, attempt, specification)
