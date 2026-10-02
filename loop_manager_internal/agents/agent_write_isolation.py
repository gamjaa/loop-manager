"""Preflight evidence for an Agent write boundary.

The provider runs directly with its native tool policy, checked by a separate
standalone sandbox preflight. Unproven calls stop before provider execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tomllib
from typing import Callable

from loop_manager_internal.agents.agent_codex_sandbox import CODEX_EXTENSION_ARGUMENTS, CodexLaunch, prepare_launch


class IsolationError(RuntimeError):
    """The requested Agent write boundary cannot be established."""


@dataclass(frozen=True)
class IsolationEvidence:
    provider: str
    cli_version: str
    environment: str
    role: str
    worktree: str
    repository_identity: str
    policy_digest: str
    status: str
    reason: str = ""
    snapshot_digest: str = ""
    configuration_digest: str = ""


@dataclass(frozen=True)
class WorktreeBoundary:
    worktree: Path
    source: Path
    other_worktrees: tuple[Path, ...]
    git_dir: Path
    common_dir: Path
    repository_identity: str


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True,
        check=False, timeout=15,
    )
    if result.returncode:
        raise IsolationError(f"Git worktree identity 확인 실패: {' '.join(args)}")
    return result.stdout.strip()


def inspect_worktree(worktree: Path, state_dir: Path,
                     *, read_only: bool = False) -> WorktreeBoundary:
    """Reject aliases, unregistered paths and a changed shared repository."""
    try:
        selected = worktree.resolve(strict=True)
        state_root = state_dir.resolve(strict=True)
        runtime = selected.is_relative_to(state_root / "runtimes")
        if not (selected.is_relative_to(state_root / "worktrees") or (read_only and runtime)):
            raise IsolationError("Agent 경로가 허용된 관리 worktree가 아닙니다.")
        root = Path(_git(selected, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if selected != root or worktree.absolute() != selected:
            raise IsolationError("Agent worktree 경로가 canonical 저장소 루트가 아닙니다.")
        common = Path(_git(selected, "rev-parse", "--path-format=absolute",
                           "--git-common-dir")).resolve(strict=True)
        git_dir = Path(_git(selected, "rev-parse", "--path-format=absolute",
                            "--absolute-git-dir")).resolve(strict=True)
        listed = _git(selected, "worktree", "list", "--porcelain")
        registered = tuple(
            Path(line[9:]).resolve(strict=True)
            for line in listed.splitlines() if line.startswith("worktree ")
        )
        if not registered or selected not in registered or selected == registered[0]:
            raise IsolationError("Agent worktree가 선택된 관리 linked worktree로 등록되지 않았습니다.")
        source = registered[0]
        source_common = Path(_git(source, "rev-parse", "--path-format=absolute",
                                  "--git-common-dir")).resolve(strict=True)
        if common != source_common or git_dir == common:
            raise IsolationError("Agent worktree의 shared Git identity가 일치하지 않습니다.")
        if runtime:
            _inspect_read_only_runtime(selected, state_root, source)
        origin = _git(selected, "remote", "get-url", "origin")
        identity = sha256((str(common) + "\0" + origin).encode()).hexdigest()
        return WorktreeBoundary(selected, source,
                                tuple(path for path in registered[1:] if path != selected),
                                git_dir, common, identity)
    except (OSError, ValueError) as error:
        raise IsolationError("Agent worktree 등록·canonical 경로 확인 실패") from error


def _inspect_read_only_runtime(selected: Path, state_root: Path, source: Path) -> None:
    """Keep the bootstrap's exact-SHA, detached runtime boundary read-only."""
    head = _git(selected, "rev-parse", "HEAD")
    repository_id = sha256(str(source).encode()).hexdigest()[:16]
    expected = state_root / "runtimes" / repository_id / head
    if (len(head) not in (40, 64) or any(char not in "0123456789abcdef" for char in head)
            or selected != expected
            or _git(selected, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
            or _git(selected, "status", "--porcelain=v1", "--untracked-files=all")):
        raise IsolationError("읽기 전용 runtime의 경로·exact SHA·detached·clean 상태가 일치하지 않습니다.")


def _configuration_digest(provider: str, worktree: Path,
                          config_path: Path | None) -> str:
    """Bind evidence to local configuration bytes without storing secrets."""
    home = Path.home()
    if provider == "codex":
        candidates = _codex_config_files(worktree)
    else:
        provider_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        candidates = (provider_home / "settings.json", worktree / ".claude/settings.json",
                      worktree / ".claude/settings.local.json")
    digest = sha256()
    for path in (*candidates, config_path):
        if path is None:
            continue
        digest.update(str(path.absolute()).encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        except OSError as error:
            raise IsolationError("Agent 설정 지문을 확인할 수 없습니다.") from error
        digest.update(b"\0")
    return digest.hexdigest()


def _codex_config_files(worktree: Path) -> tuple[Path, ...]:
    provider_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    project_configs = []
    for parent in (worktree, *worktree.parents):
        project_configs.append(parent / ".codex/config.toml")
        if (parent / ".git").exists():
            break
    return tuple(dict.fromkeys((provider_home / "config.toml", *project_configs,
            Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"),
            Path("/etc/codex/requirements.toml"))))


def _assert_no_codex_extensions(worktree: Path) -> None:
    """Reject external tools not disabled by the managed feature policy."""
    for path in _codex_config_files(worktree):
        try:
            with path.open("rb") as stream:
                configuration = tomllib.load(stream)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise IsolationError("Codex 확장 설정을 확인할 수 없습니다.") from error
        if any(configuration.get(name) for name in
               ("mcp_servers", "hooks", "notify", "profile", "profiles",
                "permissions", "default_permissions")):
            raise IsolationError(
                "Codex MCP·plugin·hook·profile·permissions 설정이 있어 쓰기 격리를 검증할 수 없습니다."
            )
        if path.name == 'requirements.toml' and any(
            configuration.get('features', {}).get(name) is True for name in ('plugins', 'apps')
        ):
            raise IsolationError('Codex 관리 정책이 확장 도구 활성화를 요구합니다.')


def _assert_no_codex_execpolicy(worktree: Path) -> None:
    """An allow rule may run a command outside the probed sandbox."""
    provider_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    for folder in (provider_home / "rules", worktree / ".codex/rules",
                   Path("/etc/codex/rules")):
        try:
            with os.scandir(folder) as entries:
                if any(entry.name.endswith(".rules") for entry in entries):
                    raise IsolationError(
                        "Codex exec-policy 규칙이 있어 sandbox 밖 실행을 배제할 수 없습니다."
                    )
        except FileNotFoundError:
            continue
        except OSError as error:
            raise IsolationError("Codex exec-policy 규칙 경로 확인 실패") from error


def _codex_configuration(read_only: bool) -> tuple[str, ...]:
    mode = "read-only" if read_only else "workspace-write"
    return (
        *CODEX_EXTENSION_ARGUMENTS,
        "-c", f'sandbox_mode="{mode}"',
        "-c", 'approvals_reviewer="auto_review"',
        "-c", "approval_policy={granular={sandbox_approval=false,request_permissions=false,"
        "rules=true,mcp_elicitations=true,skill_approval=true}}",
        "-c", "sandbox_workspace_write.writable_roots=[]",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "mcp_servers={}",
    )


def codex_arguments(read_only: bool) -> tuple[str, ...]:
    return ("--sandbox", "read-only" if read_only else "workspace-write",
            "--strict-config", "--ignore-rules", *_codex_configuration(read_only))


def prepare_isolation(provider: str, role: str, worktree: Path, state_dir: Path,
                      read_only: bool,
                      observe: Callable[[IsolationEvidence], None] | None = None,
                      snapshot_digest: Callable[[], str] | None = None,
                      *, config_path: Path | None = None,
                      expected_repository: str | None = None,
                      session_id: str | None = None,
                      environment_variables: dict[str, str] | None = None,
                      ) -> CodexLaunch:
    """Prove one physical call; emit a fresh record even on rejection."""
    environment = f"{platform.system()}:{platform.release()}:{platform.machine()}"
    boundary: WorktreeBoundary | None = None
    version = "unknown"
    args: tuple[str, ...] = ()
    launch: CodexLaunch | None = None
    status = "rejected"
    reason = ""
    snapshot = ""
    configuration = ""
    try:
        boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
        if expected_repository is None or Path(expected_repository).resolve() != boundary.source:
            raise IsolationError("선택 worktree와 등록 repository identity가 일치하지 않습니다.")
        configuration = _configuration_digest(provider, boundary.worktree, config_path)
        if snapshot_digest is None:
            raise IsolationError("Agent 시작 Git snapshot 증거가 없습니다.")
        snapshot = snapshot_digest()
        if not snapshot:
            raise IsolationError("Agent 시작 Git snapshot 지문이 비었습니다.")
        if platform.system() != "Linux":
            raise IsolationError(f"{provider} {platform.system()}에서 쓰기 격리를 검증할 수 없습니다.")
        if provider != "codex":
            raise IsolationError(f"{provider} 파일 도구와 sandbox 효력을 사전에 증명할 수 없습니다.")
        _assert_no_codex_execpolicy(boundary.worktree)
        _assert_no_codex_extensions(boundary.worktree)
        result = subprocess.run(["codex", "--version"], capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode or not result.stdout.strip():
            raise IsolationError("Codex CLI 버전을 확인할 수 없습니다.")
        version = result.stdout.strip()
        # The installed 0.159.3 baseline supports granular auto-review. Older
        # CLIs may ignore unknown overrides, so a filesystem probe alone is
        # insufficient. Direct exec still validates merged settings itself.
        match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 159, 3):
            raise IsolationError("Codex CLI가 지원되는 granular 자동 검토 버전이 아닙니다.")
        args = codex_arguments(read_only)
        if environment_variables is None:
            raise IsolationError("내장 도구 경계를 위한 필터된 Agent 환경이 없습니다.")
        try:
            launch = prepare_launch(boundary, state_dir, role, read_only, args,
                                    environment_variables)
            launch.probe()
            args = launch.arguments
        except RuntimeError as error:
            raise IsolationError(str(error)) from error
        status = "verified"
    except (IsolationError, OSError, subprocess.TimeoutExpired) as error:
        reason = str(error) if isinstance(error, IsolationError) else "격리 확인 도구 실행 실패"
    policy = json.dumps({"provider": provider, "version": version,
                         "environment": environment, "role": role,
                         "worktree": str(boundary.worktree) if boundary else str(worktree),
                         "repository": boundary.repository_identity if boundary else "",
                         "configuration": configuration,
                         "arguments": args,
                         "boundary": "native-tools-v2",
                         "probe": launch.probe_arguments if launch else (),
                         "session": session_id}, sort_keys=True)
    evidence = IsolationEvidence(
        provider, version, environment, role,
        str(boundary.worktree) if boundary else str(worktree),
        boundary.repository_identity if boundary else "",
        sha256(policy.encode()).hexdigest(), status, reason, snapshot, configuration,
    )
    if observe is not None:
        observe(evidence)
    if status != "verified":
        raise IsolationError(reason)
    assert launch is not None
    return launch


def assert_current(evidence: IsolationEvidence, worktree: Path, state_dir: Path,
                   config_path: Path, expected_repository: str | None,
                   read_only: bool, snapshot_digest: Callable[[], str]) -> None:
    """Refuse a changed workspace, settings, CLI or Git state before launch."""
    if evidence.status != "verified":
        raise IsolationError("Agent 격리 증거가 verified 상태가 아닙니다.")
    boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
    if evidence.provider == "codex":
        _assert_no_codex_execpolicy(boundary.worktree)
        _assert_no_codex_extensions(boundary.worktree)
    if (expected_repository is None or
            Path(expected_repository).resolve() != boundary.source or
            evidence.worktree != str(boundary.worktree) or
            evidence.repository_identity != boundary.repository_identity or
            evidence.configuration_digest != _configuration_digest(
                evidence.provider, boundary.worktree, config_path) or
            evidence.snapshot_digest != snapshot_digest()):
        raise IsolationError("Agent 사전 검사 뒤 worktree·설정·Git 상태가 변경됐습니다.")
    try:
        result = subprocess.run([evidence.provider, "--version"], capture_output=True,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IsolationError("Agent CLI 버전 재확인 실패") from error
    if result.returncode or result.stdout.strip() != evidence.cli_version:
        raise IsolationError("Agent CLI 버전이 사전 검사 뒤 변경됐습니다.")
