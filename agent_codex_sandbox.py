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
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(self, arguments: list[str], *,
            launch_process: Callable[..., subprocess.Popen], **kwargs):
        prompt = kwargs.pop('input')
        timeout = kwargs.pop('timeout')
        # The caller owns the atomic runtime handoff check at process creation.
        process = launch_process(arguments, stdin=subprocess.PIPE,
                                 start_new_session=True, **kwargs)
        try:
            process.communicate(prompt, timeout=timeout)
            return subprocess.CompletedProcess(arguments, process.returncode)
        finally:
            # Direct exec can still spawn tools. Reap the whole process group.
            self.stop(process)
            process.wait()

    def probe(self) -> None:
        result = subprocess.run(self.probe_arguments, cwd=json.loads(self.specification)['worktree'],
                                env=self.environment,
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode or result.stdout.strip() != 'agent-tool-boundary-v2':
            raise RuntimeError('Codex 내장 도구 sandbox의 파일 쓰기 경계를 확인할 수 없습니다.')


def _owned_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    status = path.lstat()
    if (not stat.S_ISDIR(status.st_mode) or path.resolve() != path.absolute()
            or status.st_uid != os.getuid() or status.st_mode & 0o077):
        raise RuntimeError('Codex runtime 경로의 소유권·권한을 확인할 수 없습니다.')


def _read_only_link(target: Path, link: Path) -> None:
    if not target.exists():
        if link.exists() or link.is_symlink():
            raise RuntimeError('Codex runtime 입력 경로가 변경됐습니다.')
        return
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise RuntimeError('Codex runtime 입력 연결이 변경됐습니다.')
    elif link.exists():
        raise RuntimeError('Codex runtime 입력이 읽기 전용 연결이 아닙니다.')
    else:
        link.symlink_to(target.resolve())


def prepare_launch(boundary, state_dir: Path, role: str, read_only: bool,
                   arguments: tuple[str, ...], environment: dict[str, str]) -> CodexLaunch:
    # Preserve G-088's role/repository session location for existing sessions.
    # Runtime storage belongs to Codex; it is not a writable root for its tools.
    base = state_dir.resolve() / 'agent-runtime'
    source_home = Path(environment.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
    # Validate containment before mkdir/link/mkdtemp: a misregistered state root
    # must not even create a runtime directory in the user's source checkout.
    for denied in (boundary.source, *boundary.other_worktrees, boundary.worktree,
                   boundary.common_dir, boundary.git_dir, source_home):
        resolved = denied.resolve()
        if base == resolved or resolved in base.parents or base in resolved.parents:
            raise RuntimeError('Codex runtime과 보호 경로가 겹칩니다.')
    identity = sha256((boundary.repository_identity + '\0' + str(boundary.worktree)
                       + '\0' + role).encode()).hexdigest()
    _owned_directory(base)
    runtime = base / identity
    _owned_directory(runtime)
    home = runtime / 'codex'
    _owned_directory(home)
    for name in ('auth.json', 'config.toml'):
        _read_only_link(source_home / name, home / name)
    attempt = Path(tempfile.mkdtemp(prefix='call-', dir=runtime))
    child_environment = dict(environment)
    child_environment['CODEX_HOME'] = str(home)
    child_environment['TMPDIR'] = str(attempt)
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
              '--permission-profile', 'loop-manager-tools', '--include-managed-config',
              '-c', 'permissions.loop-manager-tools.filesystem=' + filesystem,
              '-c', 'permissions.loop-manager-tools.network.enabled=false',
              '--', sys.executable, '-I', str(Path(__file__).with_name('agent_sandbox_exec.py')),
              specification)
    if not read_only:
        arguments = (*arguments, '-c',
                     'sandbox_workspace_write.writable_roots=' + json.dumps([str(attempt)]))
    return CodexLaunch(arguments, probe_arguments, child_environment, runtime, attempt, specification)
