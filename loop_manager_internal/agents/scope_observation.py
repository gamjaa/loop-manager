"""Git snapshot evidence collection through explicit Git/process ports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess

from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentChange, AgentScopeError
from loop_manager_internal.agents.agent_scope_decision import index_security_flags as _non_default_index_flags
from loop_manager_internal.verification.git_timing import snapshot_timing


_GIT_OPERATION_PATHS = (
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "BISECT_TERMS",
)

@dataclass(frozen=True)
class ObservationEffects:
    """Explicit effects supplied by the composition boundary."""

    git: Callable[..., subprocess.CompletedProcess[str]]
    git_bytes: Callable[..., bytes]
    _worktree_catalog: Callable[[Path], list[dict[str, str | bool]]]
    _handoff_run: Callable[..., subprocess.CompletedProcess[bytes]]
    run_config: Callable[..., subprocess.CompletedProcess[bytes]]


def _nul_git_paths(repo: Path, *arguments: str, effects: ObservationEffects) -> list[str]:
    return [
        os.fsdecode(record)
        for record in effects.git_bytes(repo, *arguments).split(b"\0")
        if record
    ]


def _index_entries(worktree: Path, *, effects: ObservationEffects) -> dict[str, tuple[str, ...]]:
    entries: dict[str, list[str]] = {}
    for record in effects.git_bytes(
        worktree, "ls-files", "--stage", "-v", "-z"
    ).split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RuntimeError("Git index snapshot을 해석할 수 없습니다.")
        entries.setdefault(os.fsdecode(raw_path), []).append(
            metadata.decode("ascii", errors="replace")
        )
    return {
        path: tuple(sorted(metadata))
        for path, metadata in entries.items()
    }


def _shared_ref_entries(worktree: Path, *, effects: ObservationEffects) -> dict[str, tuple[str, str]]:
    refs: dict[str, tuple[str, str]] = {}
    output = effects.git(
        worktree,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)",
    ).stdout
    for line in output.splitlines():
        fields = line.split("\0")
        if len(fields) != 3 or not fields[0]:
            raise RuntimeError("Git ref snapshot을 해석할 수 없습니다.")
        refname, objectname, symref = fields
        if refname in refs:
            raise RuntimeError(f"Git ref snapshot에 중복 ref가 있습니다: {refname}")
        refs[refname] = (objectname, symref)
    return refs


def _registered_worktree_entries(worktree: Path, *, effects: ObservationEffects) -> dict[str, tuple[str, ...]]:
    entries: dict[str, tuple[str, ...]] = {}
    active_path = _canonical_path(worktree)
    for record in effects._worktree_catalog(worktree):
        raw_path = record.get("worktree")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError("Git worktree snapshot에 경로가 없습니다.")
        path = str(_canonical_path(Path(raw_path)))
        if path in entries:
            raise RuntimeError(
                f"Git worktree snapshot에 중복 경로가 있습니다: {path}"
            )
        state = [
            f"{key}={value}" for key, value in record.items() if key != "worktree"
        ]
        if _canonical_path(Path(raw_path)) != active_path:
            state.append(f"state={_worktree_state_signature(Path(raw_path), effects=effects)}")
        entries[path] = tuple(sorted(state))
    return entries


def _normalized_config_origin(worktree: Path, raw_origin: bytes) -> bytes:
    if not raw_origin.startswith(b"file:"):
        return raw_origin
    origin_path = Path(os.fsdecode(raw_origin.removeprefix(b"file:")))
    if not origin_path.is_absolute():
        origin_path = worktree / origin_path
    return b"file:" + os.fsencode(_canonical_path(origin_path))


def _config_entries(worktree: Path, *, effects: ObservationEffects) -> dict[str, tuple[str, ...]]:
    records = effects.git_bytes(
        worktree,
        "config",
        "--null",
        "--show-origin",
        "--show-scope",
        "--list",
        "--includes",
    ).split(b"\0")
    if records and not records[-1]:
        records.pop()
    if len(records) % 3:
        raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")

    entries: dict[str, list[str]] = {}
    for offset in range(0, len(records), 3):
        raw_scope, raw_origin, definition = records[offset : offset + 3]
        raw_name, separator, raw_value = definition.partition(b"\n")
        if not raw_scope or not raw_origin or not raw_name or not separator:
            raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")
        scope = raw_scope.decode("ascii", errors="replace")
        name = raw_name.decode("utf-8", errors="replace")
        origin = _normalized_config_origin(worktree, raw_origin)
        digest = hashlib.sha256(origin + b"\0" + raw_value).hexdigest()
        entries.setdefault(f"{scope}:{name}", []).append(digest)
    snapshot = {
        key: tuple(sorted(values))
        for key, values in entries.items()
    }
    repository_exclude = Path(
        os.fsdecode(
            effects.git_bytes(worktree, "rev-parse", "--git-path", "info/exclude").strip()
        )
    )
    if not repository_exclude.is_absolute():
        repository_exclude = worktree / repository_exclude
    configured_global = effects.run_config(
        ["git", "-C", str(worktree), "config", "--path", "--get", "core.excludesFile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if configured_global.returncode not in {0, 1}:
        raise RuntimeError("Git global exclude 경로를 확인할 수 없습니다.")
    if configured_global.returncode == 0:
        global_exclude = Path(os.fsdecode(configured_global.stdout.strip()))
    else:
        global_exclude = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git/ignore"
    if not global_exclude.is_absolute():
        global_exclude = worktree / global_exclude
    for name, path in (
        ("repository-exclude", repository_exclude),
        ("global-exclude", global_exclude),
    ):
        canonical = _canonical_path(path)
        signature = _git_operation_path_signature(canonical) if canonical.exists() else "missing"
        snapshot[f"ignore-source:{name}"] = (str(canonical), signature)
    return snapshot


def _with_proven_legacy_ignore_config(
    saved: GitSnapshot,
    current_config: dict[str, tuple[str, ...]],
    *,
    effects: ObservationEffects,
) -> GitSnapshot:
    """Fill old snapshot keys only when external ignore sources have no rules."""

    keys = tuple(
        sorted(key for key in current_config if key.startswith("ignore-source:"))
    )
    if any(key.startswith("ignore-source:") for key in saved.config):
        return saved
    if len(keys) != 2:
        return saved
    for key in keys:
        path_text, signature = current_config[key]
        path = Path(path_text)
        if signature == "missing":
            if path.exists() or path.is_symlink():
                return saved
            continue
        try:
            if not path.is_file() or _git_operation_path_signature(path) != signature:
                return saved
            if any(
                line and not line.startswith(b"#")
                for line in path.read_bytes().splitlines()
            ):
                return saved
        except OSError:
            return saved
    return replace(
        saved,
        config={**saved.config, **{key: current_config[key] for key in keys}},
    )


def _git_operation_path_signature(path: Path) -> str:
    digest = hashlib.sha256()

    def include(candidate: Path, relative: str) -> None:
        stat = candidate.lstat()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_mode).encode("ascii"))
        digest.update(b"\0")
        if candidate.is_symlink():
            digest.update(os.fsencode(os.readlink(candidate)))
        elif candidate.is_file():
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)

    include(path, ".")
    if path.is_dir() and not path.is_symlink():
        for candidate in sorted(
            path.rglob("*"),
            key=lambda entry: entry.relative_to(path).as_posix(),
        ):
            include(candidate, candidate.relative_to(path).as_posix())
    return digest.hexdigest()


def _git_operation_entries(worktree: Path, *, effects: ObservationEffects) -> dict[str, str]:
    entries: dict[str, str] = {}
    arguments = tuple(value for name in _GIT_OPERATION_PATHS for value in ("--git-path", name))
    resolved_paths = effects.git(worktree, "rev-parse", *arguments).stdout.splitlines()
    # 줄바꿈을 포함하는 Git 경로는 기존 단일 조회로 해석한다.
    if len(resolved_paths) != len(_GIT_OPERATION_PATHS) or any(not path for path in resolved_paths):
        resolved_paths = [effects.git(worktree, "rev-parse", "--git-path", name).stdout.strip()
                          for name in _GIT_OPERATION_PATHS]
    for name, resolved in zip(_GIT_OPERATION_PATHS, resolved_paths):
        path = Path(resolved)
        if not path.is_absolute():
            path = worktree / path
        if path.exists() or path.is_symlink():
            entries[name] = _git_operation_path_signature(path)
    return entries


def _path_signature(worktree: Path, relative_path: str, *, effects: ObservationEffects) -> str:
    path = worktree / relative_path
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha256()
    digest.update(str(stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    try:
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            head = effects.git(path, "rev-parse", "--verify", "HEAD", check=False)
            status = effects.git(
                path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                check=False,
            )
            digest.update(head.stdout.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(status.stdout.encode("utf-8", errors="replace"))
        else:
            digest.update(f"special:{stat.st_size}".encode("ascii"))
    except OSError as error:
        digest.update(f"unreadable:{type(error).__name__}".encode("ascii"))
    return digest.hexdigest()


def _capture_local_worktree_state(worktree: Path, *, effects: ObservationEffects) -> tuple[
    str,
    str | None,
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], str]],
]:
    """Capture one worktree's HEAD, branch, index and file state."""

    head = effects.git(worktree, "rev-parse", "--verify", "HEAD").stdout.strip()
    branch_result = effects.git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    index = _index_entries(worktree, effects=effects)
    path_kinds: dict[str, set[str]] = {}
    path_groups = (
        (
            "unstaged",
            _nul_git_paths(
                worktree,
                "diff",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
                effects=effects,
            ),
        ),
        (
            "staged",
            _nul_git_paths(
                worktree,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
                effects=effects,
            ),
        ),
        (
            "untracked",
            _nul_git_paths(
                worktree,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                effects=effects,
            ),
        ),
    )
    for kind, paths in path_groups:
        for relative_path in paths:
            path_kinds.setdefault(relative_path, set()).add(kind)
    # A changed ignore rule cannot make a new file disappear from the scope
    # snapshot. Git's current exclude result is trustworthy only when the
    # matching tracked rule is still the version at HEAD.
    for relative_path in _nul_git_paths(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    , effects=effects):
        source = _ignore_rule_source(worktree, relative_path, effects=effects)
        if source is None:
            raise RuntimeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {relative_path}")
        if source == ".gitignore" or source.endswith("/.gitignore"):
            if source in path_kinds or source not in index:
                path_kinds.setdefault(relative_path, set()).add("untracked")
    for relative_path, entries in index.items():
        if _non_default_index_flags(entries):
            path_kinds.setdefault(relative_path, set()).add("index-hidden")
    dirty = {
        path: (tuple(sorted(kinds)), _path_signature(worktree, path, effects=effects))
        for path, kinds in sorted(path_kinds.items())
    }
    return head, branch, index, dirty


def _worktree_state_signature(worktree: Path, *, effects: ObservationEffects) -> str:
    if not worktree.is_dir():
        return "missing"
    head, branch, index, dirty = _capture_local_worktree_state(worktree, effects=effects)
    encoded = json.dumps(
        {
            "head": head,
            "branch": branch,
            "index": index,
            "dirty": dirty,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ignore_rule_source(worktree: Path, path: str, *, effects: ObservationEffects) -> str | None:
    result = effects._handoff_run(
        [
            "git", "-C", str(worktree), "check-ignore",
            "--no-index", "-v", "-z", "--stdin",
        ],
        input=os.fsencode(path) + b"\0",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        return None
    fields = result.stdout.split(b"\0")
    if (
        result.returncode != 0
        or len(fields) != 5
        or fields[-1]
        or fields[3] != os.fsencode(path)
    ):
        raise RuntimeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {path}")
    return os.fsdecode(fields[0])


def _normalize_legacy_ignored_snapshot(
    worktree: Path,
    saved: GitSnapshot,
    current: GitSnapshot,
    *,
    effects: ObservationEffects,
) -> GitSnapshot:
    """Discard old untracked entries only when their starting ignore rule is proven."""

    dirty = dict(saved.dirty)
    for path, (kinds, _signature) in saved.dirty.items():
        if kinds != ("untracked",) or path in current.dirty:
            continue
        if path in saved.index or path in current.index:
            continue
        source = _ignore_rule_source(worktree, path, effects=effects)
        if source is None:
            continue
        if not (source == ".gitignore" or source.endswith("/.gitignore")):
            continue
        if source not in saved.index or saved.index.get(source) != current.index.get(source):
            continue
        if source in saved.dirty or source in current.dirty:
            continue
        old_rule = effects.git(worktree, "rev-parse", f"{saved.head}:{source}", check=False)
        new_rule = effects.git(worktree, "rev-parse", f"{current.head}:{source}", check=False)
        if old_rule.returncode or new_rule.returncode or old_rule.stdout != new_rule.stdout:
            continue
        dirty.pop(path)
    return replace(saved, dirty=dirty) if dirty != saved.dirty else saved


def capture_git_snapshot(worktree: Path, *, effects: ObservationEffects) -> GitSnapshot:
    """Capture local and repository-wide Git state without staging content."""

    with snapshot_timing(effects) as observed:
        head, branch, index, dirty = _capture_local_worktree_state(worktree, effects=observed)
        return GitSnapshot(
            head=head,
            branch=branch,
            index=index,
            dirty=dirty,
            refs=_shared_ref_entries(worktree, effects=observed),
            worktrees=_registered_worktree_entries(worktree, effects=observed),
            config=_config_entries(worktree, effects=observed),
            operations=_git_operation_entries(worktree, effects=observed),
        )


def agent_snapshot_changes(
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    effects: ObservationEffects,
) -> list[AgentChange]:
    changes: dict[str, set[str]] = {}
    for path in set(before.index) | set(after.index):
        before_entries = before.index.get(path, ())
        after_entries = after.index.get(path, ())
        if before_entries != after_entries:
            changes.setdefault(path, set()).add("index")
            before_flags = _non_default_index_flags(before_entries)
            after_flags = _non_default_index_flags(after_entries)
            if before_flags != after_flags:
                changes[path].add("index flags")
    for path in set(before.dirty) | set(after.dirty):
        if before.dirty.get(path) != after.dirty.get(path):
            before_kinds = before.dirty.get(path, ((), ""))[0]
            after_kinds = after.dirty.get(path, ((), ""))[0]
            changes.setdefault(path, set()).update(before_kinds)
            changes[path].update(after_kinds)
            changes[path].add("worktree")
    if before.head != after.head:
        for path in _nul_git_paths(
            worktree,
            "diff",
            "--name-only",
            "-z",
            before.head,
            after.head,
            effects=effects,
        ):
            changes.setdefault(path, set()).add("HEAD")
    return [
        AgentChange(path, tuple(sorted(kinds)))
        for path, kinds in sorted(changes.items())
    ]


def _scope_snapshot_with_start_ignore_rules(
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    effects: ObservationEffects,
) -> GitSnapshot:
    """Keep paths hidden by a changed tracked ignore rule in scope."""

    if before.head == after.head:
        return after
    dirty = dict(after.dirty)
    for path in _nul_git_paths(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    , effects=effects):
        source = _ignore_rule_source(worktree, path, effects=effects)
        if source is None:
            raise AgentScopeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {path}")
        if source == ".gitignore" or source.endswith("/.gitignore"):
            old_rule = effects.git(worktree, "rev-parse", f"{before.head}:{source}", check=False)
            new_rule = effects.git(worktree, "rev-parse", f"{after.head}:{source}", check=False)
            if old_rule.returncode or new_rule.returncode or old_rule.stdout != new_rule.stdout:
                dirty[path] = (("untracked",), _path_signature(worktree, path, effects=effects))
    return replace(after, dirty=dirty) if dirty != after.dirty else after
