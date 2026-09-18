#!/usr/bin/env python3
"""Codex가 Loop Manager의 다음 PR을 만들게 하는 최소 부트스트랩 실행기."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
FIELDS = {
    "base_branch",
    "processed_sha",
    "active_sha",
    "phase",
    "branch",
    "worktree",
    "plan_path",
    "pr_url",
    "last_error",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    base_branch TEXT NOT NULL,
    processed_sha TEXT,
    active_sha TEXT,
    phase TEXT,
    branch TEXT,
    worktree TEXT,
    plan_path TEXT,
    pr_url TEXT,
    last_error TEXT
)
"""


def state_directory(value: str | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    if configured := os.environ.get("LOOP_MANAGER_STATE_DIR"):
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return parent / "loop-manager"


@contextlib.contextmanager
def process_lock(state_dir: Path):
    # ponytail: 부트스트랩은 전역 직렬화한다. 실제 병렬 처리 도입 시 프로젝트별 잠금으로 교체한다.
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "loop-manager.lock"
    handle = lock_path.open("a+b")
    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise RuntimeError("다른 Loop Manager 프로세스가 실행 중입니다.") from error
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def connect_database(state_dir: Path, *, create: bool = False) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError("상태 DB가 없습니다. 먼저 `loop_manager.py init`을 실행하세요.")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute(SCHEMA)
        connection.commit()
    else:
        try:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError("상태 DB를 읽을 수 없습니다. 백업을 복원하거나 다시 초기화하세요.") from error
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    if "plan_path" not in columns:
        connection.execute("ALTER TABLE projects ADD COLUMN plan_path TEXT")
        connection.commit()
    return connection


def update_project(connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    assignments = ", ".join(f"{name} = ?" for name in values)
    connection.execute(
        f"UPDATE projects SET {assignments} WHERE repo = ?",
        [*values.values(), repo],
    )
    connection.commit()


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        raise RuntimeError(f"명령 실패 ({' '.join(arguments[:3])}):\n{output}")
    return result


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["git", "-C", str(repo), *arguments], check=check)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def parse_frontmatter(text: str) -> dict[str, object]:
    lines = text.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("ExecPlan frontmatter가 없습니다.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RuntimeError("ExecPlan frontmatter 종료 구분자가 없습니다.") from error
    values: dict[str, object] = {}
    current_list: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - ") and current_list:
            assert isinstance(values[current_list], list)
            values[current_list].append(line[4:].strip())
            continue
        if ":" not in line:
            raise RuntimeError(f"유효하지 않은 frontmatter 줄입니다: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in values:
            raise RuntimeError(f"중복 frontmatter key입니다: {key}")
        if value == "":
            values[key] = []
            current_list = key
        elif value == "[]":
            values[key] = []
            current_list = None
        else:
            values[key] = value
            current_list = None
    return values


def _parse_execplan(worktree: Path, path: Path) -> dict[str, object]:
    allowed = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    values = parse_frontmatter(path.read_text(encoding="utf-8"))
    if set(values) != allowed:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
    plan_id = values["id"]
    title = values["title"]
    if not isinstance(plan_id, str) or not re.fullmatch(r"G-\d{3,}", plan_id):
        raise RuntimeError("ExecPlan id는 G-000 형식이어야 합니다.")
    if not isinstance(title, str) or path.name != f"[{plan_id}] {title}.md":
        raise RuntimeError("ExecPlan 파일명은 `[id] title.md`와 일치해야 합니다.")
    sources = values["sources"]
    dependencies = values["depends_on"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("ExecPlan sources가 하나 이상 필요합니다.")
    if not isinstance(dependencies, list):
        raise RuntimeError("ExecPlan depends_on이 유효하지 않습니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"허용되지 않는 ExecPlan source입니다: {source}")
        if not (worktree / source).is_file():
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency):
            raise RuntimeError(f"ExecPlan depends_on ID 형식이 유효하지 않습니다: {dependency}")
    if values["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("ExecPlan complexity가 유효하지 않습니다.")
    if values["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("ExecPlan risk가 유효하지 않습니다.")
    return values


def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    for plan_id, plan in catalog.items():
        dependencies = plan["depends_on"]
        assert isinstance(dependencies, list)
        missing = sorted(dependency for dependency in dependencies if dependency not in catalog)
        if missing:
            raise RuntimeError(
                f"ExecPlan {plan_id}의 존재하지 않는 depends_on ID입니다: "
                + ", ".join(missing)
            )

    states: dict[str, str] = {}
    stack: list[str] = []

    def visit(plan_id: str) -> None:
        states[plan_id] = "visiting"
        stack.append(plan_id)
        dependencies = catalog[plan_id]["depends_on"]
        assert isinstance(dependencies, list)
        for dependency in sorted(dependencies):
            if states.get(dependency) == "visiting":
                cycle_start = stack.index(dependency)
                cycle = [*stack[cycle_start:], dependency]
                raise RuntimeError("순환 ExecPlan 의존성입니다: " + " -> ".join(cycle))
            if states.get(dependency) is None:
                visit(dependency)
        stack.pop()
        states[plan_id] = "visited"

    for plan_id in catalog:
        if states.get(plan_id) is None:
            visit(plan_id)


def load_execplan_catalog(worktree: Path) -> dict[str, dict[str, object]]:
    entries: list[tuple[str, dict[str, object]]] = []
    plans_directory = worktree / "docs/plans"
    plan_paths = sorted(
        (path for path in plans_directory.glob("*.md") if path.is_file()),
        key=lambda path: path.name,
    )
    for path in plan_paths:
        plan = _parse_execplan(worktree, path)
        plan_id = plan["id"]
        assert isinstance(plan_id, str)
        entries.append((plan_id, plan))

    catalog: dict[str, dict[str, object]] = {}
    for plan_id, plan in sorted(entries, key=lambda entry: entry[0]):
        if plan_id in catalog:
            raise RuntimeError(f"중복 ExecPlan id입니다: {plan_id}")
        catalog[plan_id] = plan

    _validate_dependency_graph(catalog)
    return catalog


def validate_execplan(worktree: Path, relative_path: str) -> dict[str, object]:
    catalog = load_execplan_catalog(worktree)
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", relative_path)
    if match:
        plan = catalog.get(match.group(1))
        if plan is not None:
            expected_path = f"docs/plans/[{plan['id']}] {plan['title']}.md"
            if relative_path == expected_path:
                return plan
    raise RuntimeError(f"검증된 ExecPlan 카탈로그에 계획이 없습니다: {relative_path}")


def validate_repository(path: Path, *, allow_agents_mismatch: bool) -> Path:
    if not shutil.which("git") or not shutil.which("gh") or not shutil.which("codex"):
        raise RuntimeError("git, gh와 codex CLI가 PATH에 있어야 합니다.")
    path = path.expanduser().resolve()
    root = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != path:
        raise RuntimeError(f"저장소 루트 경로를 지정하세요: {root}")
    remote = git(root, "remote", "get-url", "origin").stdout.strip().lower()
    if not re.match(r"^(?:git@|ssh://git@|https://)(?:www\.)?github\.com[:/]", remote):
        raise RuntimeError("origin이 GitHub 저장소가 아닙니다.")
    required = [root / "AGENTS.md", root / "docs/PLANS.md", root / "docs/ARCHITECTURE.md"]
    missing = [str(item.relative_to(root)) for item in required if not item.is_file()]
    requirements = root / "docs/requirements"
    if not (root / "docs/PRODUCT.md").is_file() and not any(requirements.glob("*.md")):
        missing.append("docs/PRODUCT.md 또는 docs/requirements/*.md")
    if missing:
        raise RuntimeError("필수 파일이 없습니다: " + ", ".join(missing))
    if file_hash(root / "docs/PLANS.md") != file_hash(ROOT / "docs/PLANS.md"):
        raise RuntimeError("docs/PLANS.md가 Loop Manager 기준 파일과 다릅니다.")
    common_agents = normalized_text(ROOT / "AGENTS.md")
    target_agents = normalized_text(root / "AGENTS.md")
    if common_agents not in target_agents and not allow_agents_mismatch:
        raise RuntimeError(
            "AGENTS.md에 공통 규칙이 없습니다. 확인 후 --allow-agents-mismatch로 강제 실행하세요."
        )
    return root


def remote_base(repo: Path, requested: str | None) -> str:
    git(repo, "fetch", "origin")
    if requested:
        base = requested
    else:
        reference = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        base = reference.removeprefix("origin/")
    git(repo, "rev-parse", "--verify", f"origin/{base}")
    return base


def ensure_safe_base_worktree(repo: Path, base: str) -> None:
    if git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("기본 worktree가 dirty 상태라서 작업을 시작하지 않습니다.")
    counts = git(repo, "rev-list", "--left-right", "--count", f"HEAD...origin/{base}").stdout.split()
    if int(counts[0]):
        raise RuntimeError("기본 worktree가 원격보다 앞서 있거나 diverge되어 작업을 시작하지 않습니다.")


def worker_level(plan: dict[str, object]) -> int:
    complexity_scores = {"low": 1, "medium": 3, "high": 5}
    risk_scores = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    try:
        complexity = complexity_scores[plan["complexity"]]
        risk = risk_scores[plan["risk"]]
    except (KeyError, TypeError) as error:
        raise ValueError("검증된 ExecPlan complexity와 risk가 필요합니다.") from error
    return min(complexity + risk, 7)


def codex_model_efforts(worktree: Path | None = None) -> dict[str, frozenset[str]]:
    try:
        catalog = json.loads(
            command(
                ["codex", "debug", "models"],
                cwd=worktree,
                env=agent_environment(),
            ).stdout
        )
        models = catalog["models"]
        return {
            model["slug"]: frozenset(level["effort"] for level in model["supported_reasoning_levels"])
            for model in models
        }
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex CLI model catalog 형식이 올바르지 않습니다.") from error


def codex_profile(
    config_path: Path,
    section: str,
    name: str,
    label: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(f"Codex 설정 파일 형식이 올바르지 않습니다: {config_path}") from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if section not in parser:
        raise RuntimeError(f"{label} 설정 section [{section}]이 없습니다.")
    if name not in parser[section]:
        raise RuntimeError(f"{label} 설정이 없습니다: [{section}] {name}")

    try:
        value = parser[section][name]
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
        ) from error
    if not value.strip():
        raise RuntimeError(f"{label} Model alias/Effort 설정이 비어 있습니다.")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"{label} 설정은 `Model alias,Effort` 형식이어야 합니다.")
    alias, effort = parts
    if not alias:
        raise RuntimeError(f"{label} Model alias 설정이 비어 있습니다.")
    if not effort:
        raise RuntimeError(f"{label} Effort 설정이 비어 있습니다.")
    if "codex.models" not in parser:
        raise RuntimeError(f"{label} Model alias section [codex.models]가 없습니다.")
    if alias not in parser["codex.models"]:
        raise RuntimeError(f"{label} Model alias가 [codex.models]에 없습니다: {alias}")
    try:
        model = parser["codex.models"][alias].strip()
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [codex.models] {alias}"
        ) from error
    if not model:
        raise RuntimeError(f"{label} Model 설정이 비어 있습니다: [codex.models] {alias}")
    supported = codex_model_efforts(worktree).get(model)
    if supported is None:
        raise RuntimeError(f"{label} Model을 Codex CLI가 지원하지 않습니다: {model}")
    if effort not in supported:
        raise RuntimeError(
            f"{label} Model/Effort 조합을 Codex CLI가 지원하지 않습니다: {model},{effort}"
        )
    return model, effort


def role(config_path: Path, name: str, worktree: Path | None = None) -> tuple[str, str]:
    return codex_profile(config_path, "codex.roles", name, f"Codex {name}", worktree)


def worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 7:
        raise RuntimeError(f"Codex Worker level이 유효하지 않습니다: {level}")
    name = f"level{level}"
    return codex_profile(config_path, "codex.worker", name, f"Codex Worker {name}", worktree)


def agent_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def codex_sandbox_arguments(read_only: bool) -> list[str]:
    return ["--sandbox", "read-only"] if read_only else ["--approve-for-me"]


def run_codex(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
) -> tuple[int, str]:
    if role_name == "worker":
        if level is None:
            raise RuntimeError("Codex Worker level이 없습니다.")
        model, effort = worker_profile(config_path, level, worktree)
    else:
        if level is not None:
            raise RuntimeError(f"Codex {role_name}에 Worker level을 지정할 수 없습니다.")
        model, effort = role(config_path, role_name, worktree)
    project_dir = state_dir / "logs" / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
    project_dir.mkdir(parents=True, exist_ok=True)
    last_message = project_dir / f"{role_name}-last.txt"
    log = project_dir / f"{role_name}.log"
    last_message.unlink(missing_ok=True)
    arguments = [
        "codex",
        "exec",
        "--ephemeral",
        *codex_sandbox_arguments(read_only),
        "-C",
        str(worktree),
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-o",
        str(last_message),
        "-",
    ]
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(
            arguments,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=output,
            stderr=subprocess.STDOUT,
            env=agent_environment(),
        )
    message = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
    if result.returncode and not message:
        message = log.read_text(encoding="utf-8")[-6000:]
    return result.returncode, message.strip()


def verify(worktree: Path) -> tuple[bool, str]:
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        arguments = ["bash", str(script)]
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    result = command(arguments, cwd=worktree, check=False)
    return result.returncode == 0, result.stdout[-8000:].strip()


def pr_for_branch(repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    if url:
        result = command(["gh", "pr", "view", url, "--json", "url,state,mergedAt"], cwd=repo)
        return json.loads(result.stdout)
    result = command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", "url,state,mergedAt"],
        cwd=repo,
    )
    rows = json.loads(result.stdout)
    return rows[0] if rows else None


def make_worktree(repo: Path, worktree: Path, branch: str, base: str) -> None:
    if worktree.is_dir():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if branch_exists:
        git(repo, "worktree", "add", str(worktree), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")


def make_planning_worktree(repo: Path, worktree: Path, base: str) -> None:
    if worktree.is_dir():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(worktree), f"origin/{base}")


def changed_files(worktree: Path, since: str | None = None) -> list[str]:
    if since:
        output = git(worktree, "diff", "--name-only", f"{since}..HEAD").stdout
        return sorted(line for line in output.splitlines() if line)
    tracked = git(worktree, "diff", "--name-only").stdout.splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(tracked + untracked))


def clean_worktree(repo: Path, worktree: Path, base: str) -> bool:
    if not worktree.exists():
        return True
    if git(worktree, "status", "--porcelain").stdout.strip():
        return False
    if git(worktree, "merge-base", "--is-ancestor", "HEAD", f"origin/{base}", check=False).returncode:
        return False
    git(repo, "worktree", "remove", str(worktree))
    return True


def clear_active(connection: sqlite3.Connection, repo: str, *, processed_sha: str | None = None) -> None:
    values: dict[str, object] = {
        "active_sha": None,
        "phase": None,
        "branch": None,
        "worktree": None,
        "plan_path": None,
        "pr_url": None,
        "last_error": None,
    }
    if processed_sha is not None:
        values["processed_sha"] = processed_sha
    update_project(connection, repo, **values)


def planner_prompt(error: str | None) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Planner다.
AGENTS.md, docs/PRODUCT.md, docs/ARCHITECTURE.md, docs/PLANS.md와 기존 docs/plans를 먼저 읽어라.
현재 미구현 목표 중 가장 작고 독립적인 한 단계가 있으면 docs/PLANS.md 형식의 ExecPlan 하나만 docs/plans에 작성하라.
docs/plans 아래의 해당 파일 외에는 수정하지 마라. 구현, 테스트, commit, push와 PR 생성은 하지 마라.
추가 계획이 필요 없으면 파일을 변경하지 마라.
{failure}
""".strip()


def worker_prompt(plan_path: str, error: str | None) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Worker다.
AGENTS.md와 {plan_path}를 읽고 해당 ExecPlan 하나만 구현하라.
관련 targeted test를 작성하되 commit, push, PR 생성과 scripts/verify 실행은 하지 마라.
docs/plans에 새 ExecPlan을 만들지 말고 불필요한 후속 기능도 구현하지 마라.
{failure}
""".strip()


def reviewer_prompt(base: str) -> str:
    return f"""
origin/{base}와 현재 HEAD의 차이를 읽기 전용으로 검토하라.
AGENTS.md, 근거 PRODUCT/Requirement, ARCHITECTURE와 ExecPlan을 기준으로 correctness, 회귀, 테스트 누락, verify 약화를 확인하라.
blocking issue가 있으면 간결히 설명하고 마지막 줄을 `VERDICT: FAIL`로 끝내라.
blocking issue가 없으면 마지막 줄을 `VERDICT: PASS`로 끝내라. 파일은 수정하지 마라.
""".strip()


def run_project(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
) -> int:
    repo_path = validate_repository(Path(repo_arg), allow_agents_mismatch=allow_agents_mismatch)
    repo = str(repo_path)
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    base = row["base_branch"] if row else requested_base

    if row and row["branch"]:
        existing_pr = pr_for_branch(repo_path, row["branch"], row["pr_url"])
        if existing_pr:
            update_project(connection, repo, pr_url=existing_pr["url"])
            if existing_pr["state"] == "OPEN":
                print(f"Merge 대기 중: {existing_pr['url']}")
                return 0
            if existing_pr["state"] == "MERGED" or existing_pr.get("mergedAt"):
                git(repo_path, "fetch", "origin")
                worktree = Path(row["worktree"])
                if not clean_worktree(repo_path, worktree, base):
                    # ponytail: 부트스트랩은 안전하게 멈춘다. 후속 구현에서 cleanup_pending 큐로 분리한다.
                    update_project(connection, repo, last_error="Merge된 worktree에 미커밋 또는 미푸시 변경이 남아 있습니다.")
                    print(f"보존된 worktree를 확인하세요: {worktree}", file=sys.stderr)
                    return 1
                clear_active(connection, repo)
                print(f"Merge 확인 및 worktree 정리 완료: {existing_pr['url']}")
                return 0
            update_project(connection, repo, phase="BLOCKED", last_error="PR이 Merge 없이 닫혔습니다.")
            print(f"닫힌 PR을 다시 열어야 합니다: {existing_pr['url']}", file=sys.stderr)
            return 1

    base = remote_base(repo_path, requested_base or (row["base_branch"] if row else None))
    ensure_safe_base_worktree(repo_path, base)
    base_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()

    if not row:
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, ?)", (repo, base))
        connection.commit()
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if not row["phase"] and row["processed_sha"] == base_sha:
        print("기준 commit이 바뀌지 않아 Codex를 실행하지 않습니다.")
        return 0

    if not row["phase"]:
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
        project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
        worktree = state_dir / "worktrees" / project_id / "planning" / timestamp
        update_project(
            connection,
            repo,
            active_sha=base_sha,
            phase="PLANNING",
            branch=None,
            worktree=str(worktree),
            plan_path=None,
            pr_url=None,
            last_error=None,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    if row["phase"] == "PLANNING":
        planning_worktree = Path(row["worktree"])
        make_planning_worktree(repo_path, planning_worktree, base)
        head = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
        dirty_files = changed_files(planning_worktree)
        if head == row["active_sha"] and (not dirty_files or row["last_error"]):
            code, message = run_codex(
                planning_worktree,
                state_dir,
                config_path,
                "planner",
                planner_prompt(row["last_error"]),
                read_only=False,
            )
            if code:
                update_project(connection, repo, last_error=message or f"Planner 종료 코드 {code}")
                print(message or "Planner 실행에 실패했습니다.", file=sys.stderr)
                return 1
            update_project(connection, repo, last_error=None)
            dirty_files = changed_files(planning_worktree)

        try:
            if dirty_files:
                if len(dirty_files) != 1 or not re.fullmatch(r"docs/plans/[^/]+\.md", dirty_files[0]):
                    raise RuntimeError("Planner는 docs/plans의 ExecPlan 하나만 변경해야 합니다.")
                plan_path = dirty_files[0]
                plan = validate_execplan(planning_worktree, plan_path)
                git(planning_worktree, "add", "--", plan_path)
                git(planning_worktree, "commit", "-m", f"Add ExecPlan {plan['id']}: {plan['title']}")
            else:
                planned_files = changed_files(planning_worktree, str(row["active_sha"]))
                if not planned_files:
                    git(repo_path, "worktree", "remove", str(planning_worktree))
                    clear_active(connection, repo, processed_sha=base_sha)
                    print("추가로 계획할 작업이 없습니다.")
                    return 0
                if len(planned_files) != 1 or not re.fullmatch(r"docs/plans/[^/]+\.md", planned_files[0]):
                    raise RuntimeError("Planner commit에는 ExecPlan 하나만 있어야 합니다.")
                plan_path = planned_files[0]
                plan = validate_execplan(planning_worktree, plan_path)
        except RuntimeError as error:
            update_project(connection, repo, last_error=str(error))
            print(str(error), file=sys.stderr)
            return 1

        plan_commit = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
        git(repo_path, "fetch", "origin")
        latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
        if latest_sha not in {row["active_sha"], plan_commit}:
            published = git(
                planning_worktree,
                "merge-base",
                "--is-ancestor",
                plan_commit,
                f"origin/{base}",
                check=False,
            ).returncode == 0
            if not published:
                git(repo_path, "worktree", "remove", str(planning_worktree))
                clear_active(connection, repo)
                print("계획 중 base가 변경되어 최신 기준으로 다시 계획합니다.", file=sys.stderr)
                return 1
        if latest_sha != plan_commit:
            if latest_sha == row["active_sha"]:
                pushed = git(
                    planning_worktree,
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{base}",
                    check=False,
                )
                if pushed.returncode:
                    update_project(connection, repo, last_error=f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}")
                    print(pushed.stdout, file=sys.stderr)
                    return 1
                git(repo_path, "fetch", "origin")
                latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
                if latest_sha != plan_commit:
                    update_project(connection, repo, last_error="ExecPlan push 결과를 origin/base에서 확인할 수 없습니다.")
                    print("ExecPlan push 결과 확인에 실패했습니다.", file=sys.stderr)
                    return 1

        git(repo_path, "worktree", "remove", str(planning_worktree))
        branch = f"loop-manager/{plan['id']}"
        worktree = state_dir / "worktrees" / project_id / str(plan["id"])
        update_project(
            connection,
            repo,
            active_sha=latest_sha,
            phase="WORKING",
            branch=branch,
            worktree=str(worktree),
            plan_path=plan_path,
            last_error=None,
        )
        base_sha = latest_sha
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    branch = row["branch"]
    worktree = Path(row["worktree"])
    make_worktree(repo_path, worktree, branch, base)

    if row["phase"] in {"WORKING", "BLOCKED"}:
        plan = validate_execplan(worktree, row["plan_path"])
        level = worker_level(plan)
        code, message = run_codex(
            worktree,
            state_dir,
            config_path,
            "worker",
            worker_prompt(row["plan_path"], row["last_error"]),
            read_only=False,
            level=level,
        )
        if code:
            update_project(connection, repo, phase="WORKING", last_error=message or f"Codex 종료 코드 {code}")
            print(message or "Codex 실행에 실패했습니다.", file=sys.stderr)
            return 1
        update_project(connection, repo, phase="VERIFYING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "VERIFYING":
        passed, output = verify(worktree)
        if not passed:
            update_project(connection, repo, phase="WORKING", last_error=f"전체 검증 실패:\n{output}")
            print(output, file=sys.stderr)
            return 1

        dirty = bool(git(worktree, "status", "--porcelain").stdout.strip())
        ahead = int(git(worktree, "rev-list", "--count", f"origin/{base}..HEAD").stdout)
        if not dirty and not ahead:
            git(repo_path, "worktree", "remove", str(worktree))
            clear_active(connection, repo, processed_sha=base_sha)
            print("추가로 구현할 작업이 없습니다.")
            return 0
        if dirty:
            git(worktree, "add", "-A")
            plan = validate_execplan(worktree, row["plan_path"])
            git(worktree, "commit", "-m", f"Implement {plan['id']}: {plan['title']}")

        git(repo_path, "fetch", "origin")
        latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
        if latest_sha != row["active_sha"]:
            rebased = git(worktree, "rebase", f"origin/{base}", check=False)
            if rebased.returncode:
                update_project(connection, repo, phase="WORKING", last_error=f"rebase 실패:\n{rebased.stdout[-8000:]}")
                print(rebased.stdout, file=sys.stderr)
                return 1
            passed, output = verify(worktree)
            if not passed:
                update_project(connection, repo, phase="WORKING", last_error=f"rebase 후 전체 검증 실패:\n{output}")
                print(output, file=sys.stderr)
                return 1
            update_project(connection, repo, active_sha=latest_sha)
        update_project(connection, repo, phase="REVIEWING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "REVIEWING":
        code, review = run_codex(
            worktree,
            state_dir,
            config_path,
            "reviewer",
            reviewer_prompt(base),
            read_only=True,
        )
        passed = code == 0 and re.search(r"(?mi)^VERDICT:\s*PASS\s*$", review) and not re.search(
            r"(?mi)^VERDICT:\s*FAIL\s*$", review
        )
        if not passed:
            update_project(connection, repo, phase="WORKING", last_error=f"Review 실패:\n{review[-8000:]}")
            print(review or "Reviewer 실행에 실패했습니다.", file=sys.stderr)
            return 1
        update_project(connection, repo, phase="PUBLISHING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "PUBLISHING":
        existing_pr = pr_for_branch(repo_path, branch)
        if not existing_pr:
            git(worktree, "push", "-u", "origin", branch)
            title = git(worktree, "log", "-1", "--pretty=%s").stdout.strip()
            created = command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    "Loop Manager 최소 부트스트랩 실행기가 생성한 PR입니다.",
                ],
                cwd=worktree,
            )
            url = created.stdout.strip().splitlines()[-1]
        else:
            url = str(existing_pr["url"])
        update_project(connection, repo, phase="PR_OPEN", pr_url=url, last_error=None)
        print(f"PR 생성 완료: {url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="기본 운영체제 상태 디렉터리 대신 사용할 경로")
    parser.add_argument("--config", default=str(ROOT / "config/config.ini"), help="config.ini 경로")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="상태 디렉터리와 SQLite를 초기화")
    run = subcommands.add_parser("run", help="프로젝트에서 다음 최소 작업 하나를 처리")
    run.add_argument("repository", help="대상 Git 저장소 루트")
    run.add_argument("--base", help="기준 branch. 생략하면 origin/HEAD 사용")
    run.add_argument(
        "--allow-agents-mismatch",
        action="store_true",
        help="공통 AGENTS.md가 포함되지 않은 프로젝트를 확인 후 강제 실행",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = state_directory(args.state_dir)
    try:
        with process_lock(state_dir):
            if args.command == "init":
                with connect_database(state_dir, create=True):
                    pass
                print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
                return 0
            with connect_database(state_dir) as connection:
                return run_project(
                    connection,
                    state_dir,
                    args.repository,
                    Path(args.config).expanduser().resolve(),
                    args.base,
                    args.allow_agents_mismatch,
                )
    except (RuntimeError, OSError, sqlite3.DatabaseError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
