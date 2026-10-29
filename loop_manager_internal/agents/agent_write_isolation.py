"""Invocation-bound write policies and evidence.

Codex uses a standalone native sandbox probe. Claude records documented native
configuration as prepared, separately from actual invocation observations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tomllib
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_claude_preflight import ClaudeLaunch

from loop_manager_internal.agents.agent_codex_sandbox import CODEX_EXTENSION_ARGUMENTS, CodexLaunch, prepare_launch
from loop_manager_internal.config import execution_path
from loop_manager_internal.execution.validation_feedback import redact_secrets


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
    stable_configuration_digest: str = ""


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
        encoding="utf-8", check=False, timeout=15,
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
    if selected.parent.parent.name == 'commands':
        from loop_manager_internal.runtime.runtime_identity import expected_runtime_worktree, _read_runtime_intent
        mutation_id = selected.parent.name
        try:
            expected = expected_runtime_worktree(state_root, source, head, command_id=mutation_id)
            created, reason = _read_runtime_intent(state_root, mutation_id, source, selected, head)
            records = _git(source, 'worktree', 'list', '--porcelain').split('\n\n')
            marker = next((record.splitlines() for record in records
                           if record.startswith('worktree ')
                           and Path(record.splitlines()[0][9:]).resolve() == selected), ())
            if not created or reason != 'loop-manager-runtime:' + mutation_id or 'locked ' + reason not in marker:
                raise IsolationError('수동 runtime의 durable 소유 표식이 다릅니다.')
        except (RuntimeError, OSError) as error:
            raise IsolationError('수동 runtime의 durable 소유권을 확인할 수 없습니다.') from error
    if (len(head) not in (40, 64) or any(char not in "0123456789abcdef" for char in head)
            or selected != expected
            or _git(selected, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
            or _git(selected, "status", "--porcelain=v1", "--untracked-files=all")):
        raise IsolationError("읽기 전용 runtime의 경로·exact SHA·detached·clean 상태가 일치하지 않습니다.")


def _configuration_digest(provider: str, worktree: Path,
                          config_path: Path | None,
                          environment_variables: dict[str, str] | None = None,
                          *, exclude_common_codex_home: bool = False) -> str:
    """Bind evidence to local configuration bytes without storing secrets."""
    variables = os.environ if environment_variables is None else environment_variables
    home = Path(variables.get('HOME', variables.get('USERPROFILE', str(Path.home()))))
    if provider == "codex":
        candidates = (_codex_config_files(worktree) if environment_variables is None
                      else _codex_config_files(worktree, environment_variables))
        if exclude_common_codex_home:
            common_home = Path(variables.get("CODEX_HOME", home / ".codex")) / "config.toml"
            candidates = tuple(path for path in candidates if path != common_home)
    else:
        provider_home = Path(variables.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        candidates = (provider_home / "settings.json", worktree / ".claude/settings.json",
                      worktree / ".claude/settings.local.json")
    digest = sha256()
    variable, filename = (('CODEX_HOME', 'auth.json') if provider == 'codex'
                          else ('CLAUDE_CONFIG_DIR', '.credentials.json'))
    auth_home = Path(variables.get(variable, home / ('.codex' if provider == 'codex' else '.claude')))
    digest.update(str((auth_home / filename).resolve()).encode())  # 인증값 대신 원본 경로만 결합한다.
    for path in (*candidates, config_path):
        if path is None:
            continue
        digest.update(str(path.absolute()).encode())
        digest.update(str(path.resolve()).encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        except OSError as error:
            raise IsolationError("Agent 설정 지문을 확인할 수 없습니다.") from error
        digest.update(b"\0")
    return digest.hexdigest()


def _stable_configuration_digest(provider: str, worktree: Path,
                                 config_path: Path | None,
                                 environment_variables: dict[str, str]) -> str:
    """Fingerprint configuration that native Codex lookups may not replace."""
    return _configuration_digest(provider, worktree, config_path, environment_variables,
                                 exclude_common_codex_home=(provider == 'codex'))


def _codex_config_files(worktree: Path, environment_variables: dict[str, str] | None = None) -> tuple[Path, ...]:
    variables = os.environ if environment_variables is None else environment_variables
    home = Path(variables.get('HOME', variables.get('USERPROFILE', str(Path.home()))))
    provider_home = Path(variables.get("CODEX_HOME", home / ".codex"))
    project_configs = []
    for parent in (worktree, *worktree.parents):
        project_configs.append(parent / ".codex/config.toml")
        if (parent / ".git").exists():
            break
    return tuple(dict.fromkeys((provider_home / "config.toml", *project_configs,
            Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"),
            Path("/etc/codex/requirements.toml"))))


def _assert_no_codex_extensions(worktree: Path, environment_variables: dict[str, str] | None = None) -> None:
    """Reject external tools not disabled by the managed feature policy."""
    for path in (_codex_config_files(worktree) if environment_variables is None
                 else _codex_config_files(worktree, environment_variables)):
        try:
            with path.open("rb") as stream:
                configuration = tomllib.load(stream)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise IsolationError("Codex 확장 설정을 확인할 수 없습니다.") from error
        if os.name != 'nt' and configuration.get('cli_auth_credentials_store') not in (None, 'file'):
            # 기본 사용자 홈과 공통 홈의 경로 의존 인증 공유는 검증되지 않았다.
            raise IsolationError('Codex 자동 실행 홈에서 파일 외 인증 저장소 재사용을 확인할 수 없습니다. '
                                 '전용 CODEX_HOME의 파일 인증을 준비하고 조직 정책은 유지하세요.')
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


def _assert_no_codex_execpolicy(worktree: Path, environment_variables: dict[str, str] | None = None) -> None:
    """An allow rule may run a command outside the probed sandbox."""
    variables = os.environ if environment_variables is None else environment_variables
    home = Path(variables.get('HOME', variables.get('USERPROFILE', str(Path.home()))))
    provider_home = Path(variables.get("CODEX_HOME", home / ".codex"))
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


def _check_codex_configuration(worktree: Path, state_dir: Path,
                               environment_variables: dict[str, str]) -> None:
    source_environment = dict(environment_variables,
                              CODEX_HOME=str(state_dir.resolve() / 'automation-codex'))
    _assert_no_codex_execpolicy(worktree, source_environment)
    _assert_no_codex_extensions(worktree, source_environment)
    _assert_no_codex_execpolicy(worktree, environment_variables)
    _assert_no_codex_extensions(worktree, environment_variables)


def _checked_codex_configuration(worktree: Path, state_dir: Path,
                                 config_path: Path | None,
                                 environment_variables: dict[str, str]) -> tuple[str, str]:
    before = _configuration_digest('codex', worktree, config_path, environment_variables)
    stable_before = _stable_configuration_digest(
        'codex', worktree, config_path, environment_variables)
    _check_codex_configuration(worktree, state_dir, environment_variables)
    current = _configuration_digest('codex', worktree, config_path, environment_variables)
    stable_current = _stable_configuration_digest(
        'codex', worktree, config_path, environment_variables)
    if current != before or stable_current != stable_before:
        raise IsolationError('Codex 설정이 격리 검사 중 변경됐습니다.')
    return current, stable_current


def _policy_digest(provider: str, version: str, environment: str, role: str,
                   worktree: str, repository: str, configuration: str,
                   arguments: tuple[str, ...], launch: CodexLaunch | ClaudeLaunch | None,
                   session_id: str | None) -> str:
    policy = json.dumps({"provider": provider, "version": version,
                         "environment": environment, "role": role,
                         "worktree": worktree, "repository": repository,
                         "configuration": configuration,
                         "arguments": arguments,
                         "boundary": "claude-native-request-v1" if provider == 'claude' else "native-tools-v2",
                         "probe": launch.probe_arguments if launch else (),
                         "session": session_id}, sort_keys=True)
    return sha256(policy.encode()).hexdigest()


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


def prepare_execution_environment(provider: str, role: str, worktree: Path, state_dir: Path,
                                  read_only: bool, expected_repository: str | None,
                                  environment_variables: dict[str, str]) -> CodexLaunch:
    """Select owned storage before any provider CLI or profile lookup."""
    boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
    if expected_repository is None or Path(expected_repository).resolve() != boundary.source:
        raise IsolationError('선택 worktree와 등록 repository identity가 일치하지 않습니다.')
    if provider != 'codex':
        raise IsolationError(f'{provider} 파일 도구와 sandbox 효력을 사전에 증명할 수 없습니다.')
    try:
        return prepare_launch(boundary, state_dir, role, read_only,
                              codex_arguments(read_only), dict(environment_variables))
    except RuntimeError as error:
        raise IsolationError(execution_path.append_path_recovery_hint(str(error))) from error


def prepare_isolation(provider: str, role: str, worktree: Path, state_dir: Path,
                      read_only: bool,
                      observe: Callable[[IsolationEvidence], None] | None = None,
                      snapshot_digest: Callable[[], str] | None = None,
                      *, config_path: Path | None = None,
                      expected_repository: str | None = None,
                      session_id: str | None = None,
                      environment_variables: dict[str, str] | None = None,
                      prepared_launch: CodexLaunch | ClaudeLaunch | None = None,
                      ) -> CodexLaunch | ClaudeLaunch:
    """Prepare one physical call; emit a fresh record even on rejection."""
    if provider == 'claude':
        from .agent_claude_preflight import isolate
        return isolate(role, worktree, state_dir, read_only, observe, snapshot_digest,
            config_path=config_path, expected_repository=expected_repository, session_id=session_id,
            environment_variables=environment_variables, prepared_launch=prepared_launch)
    environment = f"{platform.system()}:{platform.release()}:{platform.machine()}"
    boundary: WorktreeBoundary | None = None
    version = "unknown"
    args: tuple[str, ...] = ()
    launch: CodexLaunch | None = None
    status = "rejected"
    reason = ""
    snapshot = ""
    configuration = ""
    stable_configuration = ""
    try:
        boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
        if expected_repository is None or Path(expected_repository).resolve() != boundary.source:
            raise IsolationError("선택 worktree와 등록 repository identity가 일치하지 않습니다.")
        if snapshot_digest is None:
            raise IsolationError("Agent 시작 Git snapshot 증거가 없습니다.")
        snapshot = snapshot_digest()
        if not snapshot:
            raise IsolationError("Agent 시작 Git snapshot 지문이 비었습니다.")
        if platform.system() not in {"Linux", "Darwin", "Windows"}:
            raise IsolationError(f"{provider} {platform.system()}에서 쓰기 격리를 검증할 수 없습니다.")
        if provider != "codex":
            raise IsolationError(f"{provider} 파일 도구와 sandbox 효력을 사전에 증명할 수 없습니다.")
        if environment_variables is None:
            raise IsolationError("내장 도구 경계를 위한 필터된 Agent 환경이 없습니다.")
        try:
            launch = prepared_launch or prepare_launch(boundary, state_dir, role, read_only,
                                                       codex_arguments(read_only), environment_variables)
        except RuntimeError as error:
            raise IsolationError(execution_path.append_path_recovery_hint(str(error))) from error
        selected_environment = launch.environment
        configuration, stable_configuration = _checked_codex_configuration(
            boundary.worktree, state_dir, config_path, selected_environment)
        def check_preparation_lookup() -> tuple[str, str]:
            current_configuration, current_stable = _checked_codex_configuration(
                boundary.worktree, state_dir, config_path, selected_environment)
            if current_stable != stable_configuration:
                raise IsolationError('관리·프로젝트 Codex 설정이 준비 중 변경됐습니다.')
            return current_configuration, current_stable

        result = subprocess.run(["codex", "--version"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
                                env=selected_environment)
        if result.returncode or not result.stdout.strip():
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if execution_path.command_lookup_failed(output):
                raise IsolationError(execution_path.append_path_recovery_hint(
                    redact_secrets(
                        f"Codex CLI version check failed (exit {result.returncode}):\n{output[-6000:]}"
                    )))
            raise IsolationError("Codex CLI 버전을 확인할 수 없습니다.")
        version = result.stdout.strip()
        configuration, _ = check_preparation_lookup()
        # The installed 0.159.3 baseline supports granular auto-review. Older
        # CLIs may ignore unknown overrides, so a filesystem probe alone is
        # insufficient. Direct exec still validates merged settings itself.
        match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 159, 3):
            raise IsolationError("Codex CLI가 지원되는 granular 자동 검토 버전이 아닙니다.")
        try:
            launch.probe(on_features_checked=check_preparation_lookup)
            args = launch.arguments
        except RuntimeError as error:
            message = execution_path.append_path_recovery_hint(str(error))
            raise IsolationError(message) from error
        configuration, _ = check_preparation_lookup()
        status = "verified"
    except (IsolationError, OSError, subprocess.TimeoutExpired) as error:
        if isinstance(error, IsolationError):
            reason = str(error)
        elif execution_path.missing_executable(error, provider):
            reason = execution_path.with_path_recovery_hint(
                f"{provider.capitalize()} CLI 실행 실패: {error}")
        else:
            reason = "격리 확인 도구 실행 실패"
    canonical_worktree = str(boundary.worktree) if boundary else str(worktree)
    repository_identity = boundary.repository_identity if boundary else ""
    evidence = IsolationEvidence(
        provider, version, environment, role, canonical_worktree,
        repository_identity,
        _policy_digest(provider, version, environment, role, canonical_worktree,
                       repository_identity, configuration, args, launch, session_id),
        status, reason, snapshot, configuration,
        stable_configuration_digest=stable_configuration,
    )
    if observe is not None:
        observe(evidence)
    if status != "verified":
        raise IsolationError(reason)
    assert launch is not None
    return launch


def refresh_configuration_evidence(evidence: IsolationEvidence, launch: CodexLaunch,
                                   worktree: Path, state_dir: Path, config_path: Path,
                                   expected_repository: str | None, read_only: bool,
                                   snapshot_digest: Callable[[], str], *, session_id: str | None = None,
                                   instruction_configuration_digest: str | None = None,
                                   environment_variables: dict[str, str] | None = None
                                   ) -> IsolationEvidence:
    """Bind safe settings written by preparation lookups before the final check."""
    if (evidence.provider != 'codex' or evidence.status != 'verified' or
            environment_variables is None or launch.environment != environment_variables):
        raise IsolationError('Codex 설정을 다시 검사할 사전 증거가 없습니다.')
    boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
    if (expected_repository is None or Path(expected_repository).resolve() != boundary.source or
            evidence.worktree != str(boundary.worktree) or
            evidence.repository_identity != boundary.repository_identity or
            evidence.snapshot_digest != snapshot_digest()):
        raise IsolationError('Agent 준비 중 worktree·repository·Git 상태가 변경됐습니다.')
    configuration, stable_configuration = _checked_codex_configuration(
        boundary.worktree, state_dir, config_path, environment_variables)
    if (instruction_configuration_digest is not None and
            configuration != instruction_configuration_digest):
        raise IsolationError('Codex 설정이 지침 해석 뒤 변경됐습니다.')
    if stable_configuration != evidence.stable_configuration_digest:
        raise IsolationError('관리·프로젝트 Codex 설정이 준비 중 변경됐습니다.')
    if configuration == evidence.configuration_digest:
        return evidence
    return replace(evidence, configuration_digest=configuration,
                   policy_digest=_policy_digest(
                       evidence.provider, evidence.cli_version, evidence.environment,
                       evidence.role, evidence.worktree, evidence.repository_identity,
                       configuration, launch.arguments, launch, session_id))


def assert_preparation_lookup_current(evidence: IsolationEvidence, worktree: Path,
                                     state_dir: Path, config_path: Path, *,
                                     environment_variables: dict[str, str] | None = None) -> str:
    """Reject policy or manager-input drift and return the current config identity."""
    if (evidence.provider != 'codex' or evidence.status != 'verified' or
            not evidence.stable_configuration_digest or environment_variables is None):
        raise IsolationError('Codex 준비 조회를 다시 검사할 사전 증거가 없습니다.')
    configuration, stable_configuration = _checked_codex_configuration(
        worktree, state_dir, config_path, environment_variables)
    if stable_configuration != evidence.stable_configuration_digest:
        raise IsolationError('관리·프로젝트 Codex 설정이 준비 중 변경됐습니다.')
    return configuration


def assert_current_configuration(evidence: IsolationEvidence, worktree: Path, state_dir: Path,
                                config_path: Path, expected_repository: str | None,
                                read_only: bool, snapshot_digest: Callable[[], str], *,
                                environment_variables: dict[str, str] | None = None) -> None:
    """Recheck launch inputs without another CLI lookup at the process boundary."""
    if evidence.status != 'verified':
        raise IsolationError('Agent 격리 증거가 verified 상태가 아닙니다.')
    boundary = inspect_worktree(worktree, state_dir, read_only=read_only)
    selected_environment = environment_variables
    if evidence.provider == 'codex':
        selected_environment = dict(os.environ if environment_variables is None else environment_variables)
        _check_codex_configuration(boundary.worktree, state_dir, selected_environment)
    if (expected_repository is None or
            Path(expected_repository).resolve() != boundary.source or
            evidence.worktree != str(boundary.worktree) or
            evidence.repository_identity != boundary.repository_identity or
            evidence.configuration_digest != _configuration_digest(
                evidence.provider, boundary.worktree, config_path, selected_environment) or
            evidence.snapshot_digest != snapshot_digest()):
        raise IsolationError('Agent 사전 검사 뒤 worktree·설정·Git 상태가 변경됐습니다.')


def assert_current(evidence: IsolationEvidence, worktree: Path, state_dir: Path,
                   config_path: Path, expected_repository: str | None,
                   read_only: bool, snapshot_digest: Callable[[], str],
                   *, environment_variables: dict[str, str] | None = None) -> None:
    """Refuse a changed workspace, settings, CLI or Git state before launch."""
    if evidence.status != "verified":
        raise IsolationError("Agent 격리 증거가 verified 상태가 아닙니다.")
    try:
        result = subprocess.run([evidence.provider, "--version"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
                                env=environment_variables)
    except (OSError, subprocess.TimeoutExpired) as error:
        if isinstance(error, OSError) and execution_path.missing_executable(error, evidence.provider):
            raise IsolationError(execution_path.with_path_recovery_hint(
                f"Agent CLI 실행 실패: {error}")) from error
        raise IsolationError("Agent CLI 버전 재확인 실패") from error
    if result.returncode or result.stdout.strip() != evidence.cli_version:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if execution_path.command_lookup_failed(output):
            raise IsolationError(execution_path.append_path_recovery_hint(
                redact_secrets(
                    f"Agent CLI version check failed (exit {result.returncode}):\n{output[-6000:]}"
                )))
        raise IsolationError("Agent CLI 버전이 사전 검사 뒤 변경됐습니다.")
    assert_current_configuration(evidence, worktree, state_dir, config_path,
                                 expected_repository, read_only, snapshot_digest,
                                 environment_variables=environment_variables)
