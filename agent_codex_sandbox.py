"""Native Codex sandbox envelope for the complete CLI process tree."""
from __future__ import annotations

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
    prefix: tuple[str, ...]
    environment: dict[str, str]
    runtime: Path
    attempt: Path
    specification: str

    def wrap(self, arguments: list[str]) -> list[str]:
        return [*self.prefix, *arguments]

    @staticmethod
    def stop(process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(self, arguments: list[str], **kwargs):
        prompt = kwargs.pop('input')
        timeout = kwargs.pop('timeout')
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE,
                                   start_new_session=True, **kwargs)
        try:
            process.communicate(prompt, timeout=timeout)
            return subprocess.CompletedProcess(arguments, process.returncode)
        finally:
            # The native sandbox is an extra parent process. Terminate its whole
            # process group on timeout/interruption, including background tools.
            self.stop(process)
            process.wait()

    def probe(self) -> None:
        result = subprocess.run(self.prefix, cwd=self.runtime, env=self.environment,
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode or result.stdout.strip() != 'agent-write-boundary-v1':
            raise RuntimeError('Codex 전체 CLI sandbox의 mount·파일 쓰기 경계를 확인할 수 없습니다.')


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
    # Dedicated role/repository session storage; authentication remains a RO link
    # to its existing location, never copied or granted write access.
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
    writable = [home, attempt]
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
    specification = json.dumps({'writable': [str(path) for path in writable],
                                'protected': [str(path) for path in protected],
                                'files': [str(path) for path in files],
                                'worktree': str(boundary.worktree)}, sort_keys=True)
    # The outer native sandbox permits provider API traffic. Model shell
    # commands retain their own inner network_access=false and granular review.
    prefix = ('codex', 'sandbox', '-C', str(attempt),
              '-c', 'sandbox_mode="workspace-write"',
              '-c', 'sandbox_workspace_write.writable_roots=' + json.dumps([str(p) for p in writable]),
              '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
              '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
              '-c', 'sandbox_workspace_write.network_access=true',
              '--', sys.executable, '-I', str(Path(__file__).with_name('agent_sandbox_exec.py')),
              specification)
    if not read_only:
        arguments = (*arguments, '-c',
                     'sandbox_workspace_write.writable_roots=' + json.dumps([str(attempt)]))
    return CodexLaunch(arguments, prefix, child_environment, runtime, attempt, specification)
