"""Git snapshot evidence collection through explicit Git/process ports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import json
import os
import re
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

RAW_DIFF_OPTIONS = ("--raw", "--no-abbrev", "--no-renames", "--no-ext-diff",
                    "--no-textconv", "--ignore-submodules=none", "-z")
_INDEX_DEBUG = re.compile(
    rb"  ctime: [^\n]*\n  mtime: [^\n]*\n  dev: [^\n]*\n"
    rb"  uid: [^\n]*\n  size: [^\n]*\tflags: [0-9a-fA-F]+\n"
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
    raw = effects.git_bytes(repo, *arguments)
    if raw and not raw.endswith(b"\0"):
        raise RuntimeError("Git 경로 목록이 잘렸습니다.")
    return [
        record.decode("utf-8", errors="surrogateescape")
        for record in raw.split(b"\0")
        if record
    ]


def _unstaged_git_paths(worktree: Path, *, effects: ObservationEffects) -> list[str]:
    # name-only can report every stat-stale entry when read-only diff cannot
    # refresh the index. Numstat checks content without writing the index.
    raw = effects.git_bytes(worktree, 'diff', '--numstat', '--no-renames',
                            '--no-ext-diff', '--no-textconv', '-z',
                            '--ignore-submodules=none', '--')
    if raw and not raw.endswith(b'\0'):
        raise RuntimeError('Git unstaged numstat 목록이 잘렸습니다.')
    paths = []
    for record in raw.split(b'\0')[:-1]:
        fields = record.split(b'\t', 2)
        if (len(fields) != 3 or not fields[2]
                or not ((fields[0].isdigit() and fields[1].isdigit())
                        or fields[:2] == [b'-', b'-'])):
            raise RuntimeError('Git unstaged numstat 목록을 해석할 수 없습니다.')
        paths.append(os.fsdecode(fields[2]))
    return paths


def _index_entries(worktree: Path, *, effects: ObservationEffects,
                   details: dict | None = None) -> dict[str, tuple[str, ...]]:
    entries: dict[str, list[str]] = {}
    raw = effects.git_bytes(worktree, "ls-files", "--stage", "-v",
                            *(("--debug",) if details is not None else ()), "-z")
    debug_records = []
    position = 0
    while position < len(raw):
        end = raw.find(b"\0", position)
        if end < 0:
            raise RuntimeError("Git index snapshot이 잘렸습니다.")
        record = raw[position:end]
        position = end + 1
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator or not raw_path:
            raise RuntimeError("Git index snapshot을 해석할 수 없습니다.")
        entries.setdefault(raw_path.decode("utf-8", errors="surrogateescape"), []).append(
            metadata.decode("ascii", errors="replace")
        )
        if details is not None:
            debug = _INDEX_DEBUG.match(raw, position)
            if debug is None:
                raise RuntimeError("Git index debug snapshot을 해석할 수 없습니다.")
            debug_records.append(raw_path + b"\0" + debug.group())
            position = debug.end()
    if details is not None:
        details["index_debug"] = b"".join(debug_records).decode("utf-8", errors="surrogateescape")
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
    if configured_global.returncode == 0 and not configured_global.stdout.strip():
        # 빈 core.excludesFile은 전역 ignore를 끈다. Path('')는 worktree가 된다.
        global_exclude = None
    elif configured_global.returncode == 0:
        global_exclude = Path(os.fsdecode(configured_global.stdout.strip()))
    else:
        global_exclude = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git/ignore"
    if global_exclude is not None and not global_exclude.is_absolute():
        global_exclude = worktree / global_exclude
    for name, path in (
        ("repository-exclude", repository_exclude),
        ("global-exclude", global_exclude),
    ):
        if path is None:
            snapshot[f"ignore-source:{name}"] = ("", "disabled")
            continue
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


def _git_operation_entries(worktree: Path, *, effects: ObservationEffects,
                           paths: dict | None = None) -> dict[str, str]:
    entries: dict[str, str] = {}
    arguments = tuple(value for name in _GIT_OPERATION_PATHS for value in ("--git-path", name))
    resolved_paths = effects.git(worktree, "rev-parse", *arguments).stdout.splitlines()
    # 줄바꿈을 포함하는 Git 경로는 기존 단일 조회로 해석한다.
    if len(resolved_paths) != len(_GIT_OPERATION_PATHS) or any(not path for path in resolved_paths):
        resolved_paths = [effects.git(worktree, "rev-parse", "--git-path", name).stdout.strip()
                          for name in _GIT_OPERATION_PATHS]
    if not all(resolved_paths):
        raise RuntimeError("Git operation 경로를 확인할 수 없습니다.")
    for name, resolved in zip(_GIT_OPERATION_PATHS, resolved_paths):
        path = Path(resolved)
        if not path.is_absolute():
            path = worktree / path
        if paths is not None:
            paths[name] = path
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
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
            )
            digest.update(head.stdout.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(status.stdout.encode("utf-8", errors="replace"))
        else:
            digest.update(f"special:{stat.st_size}".encode("ascii"))
    except OSError as error:
        digest.update(f"unreadable:{type(error).__name__}".encode("ascii"))
    return digest.hexdigest()


def _capture_local_worktree_state(worktree: Path, *, effects: ObservationEffects,
                                  details: dict | None = None) -> tuple[
    str,
    str | None,
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], str]],
]:
    """Capture one worktree's HEAD, branch, index and file state."""

    identity = effects.git(worktree, "rev-parse", "HEAD", "--abbrev-ref", "HEAD").stdout.splitlines()
    if (len(identity) != 2 or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", identity[0]) is None
            or not identity[1]):
        raise RuntimeError("Git HEAD·branch snapshot을 해석할 수 없습니다.")
    head, branch = identity
    branch = None if branch == "HEAD" else branch
    index = _index_entries(worktree, effects=effects, details=details)
    if details is not None:
        raw = effects.git_bytes(worktree, "diff", "--cached", *RAW_DIFF_OPTIONS, "--")
        records = raw.split(b"\0")
        if records.pop() or len(records) % 2 or any(
                not records[n].startswith(b":") or not records[n + 1]
                for n in range(0, len(records), 2)):
            raise RuntimeError("Git staged diff snapshot을 해석할 수 없습니다.")
        staged_paths = [os.fsdecode(path) for path in records[1::2]]
        details["staged_raw"] = os.fsdecode(raw)
    else:
        staged_paths = _nul_git_paths(worktree, "diff", "--cached", "--name-only",
                                      "-z", "--ignore-submodules=none", effects=effects)
    path_kinds: dict[str, set[str]] = {}
    path_groups = (
        (
            "unstaged",
            _unstaged_git_paths(worktree, effects=effects),
        ),
        (
            "staged",
            staged_paths,
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
    ignored = _nul_git_paths(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    , effects=effects)
    if details is not None:
        details["ignored_paths"] = tuple(ignored)
    sources = _ignore_rule_sources(worktree, ignored, effects=effects)
    for relative_path in ignored:
        source = sources.get(relative_path)
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


def _ignore_rule_sources(worktree: Path, paths: list[str], *, effects: ObservationEffects) -> dict[str, str]:
    if not paths:
        return {}
    result = effects._handoff_run(
        [
            "git", "-C", str(worktree), "check-ignore",
            "--no-index", "-v", "-z", "--stdin",
        ],
        input=b"".join(os.fsencode(path) + b"\0" for path in paths),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fields = result.stdout.split(b"\0")
    if result.returncode not in {0, 1} or fields.pop() or len(fields) % 4:
        raise RuntimeError("ignored 경로의 Git 규칙 묶음을 확인할 수 없습니다.")
    sources = {}
    matched = set()
    expected = set(paths)
    for offset in range(0, len(fields), 4):
        source, line, pattern, raw_path = fields[offset:offset + 4]
        path = os.fsdecode(raw_path)
        if path not in expected or path in matched or not source or not line or not pattern:
            raise RuntimeError("ignored 경로의 Git 규칙 묶음이 불완전합니다.")
        matched.add(path)
        # Verbose check-ignore also reports the last matching negation rule.
        # Such a path remains authoritative, including after its deletion.
        if not pattern.startswith(b'!'):
            sources[path] = os.fsdecode(source)
    if bool(matched) != (result.returncode == 0):
        raise RuntimeError("ignored 경로의 Git 규칙 결과가 종료 코드와 다릅니다.")
    return sources


def _ignore_rule_source(worktree: Path, path: str, *, effects: ObservationEffects) -> str | None:
    return _ignore_rule_sources(worktree, [path], effects=effects).get(path)


def _normalize_legacy_ignored_snapshot(
    worktree: Path,
    saved: GitSnapshot,
    current: GitSnapshot,
    *,
    effects: ObservationEffects,
    start: GitSnapshot | None = None,
) -> GitSnapshot:
    """Build a comparison view, retaining the original invocation evidence."""

    start = start or saved

    dirty = dict(saved.dirty)
    for path, (kinds, _signature) in saved.dirty.items():
        if kinds != ("untracked",) or path in current.dirty:
            continue
        if path in start.index or path in saved.index or path in current.index:
            continue
        source = _ignore_rule_source(worktree, path, effects=effects)
        if source is None:
            continue
        if not (source in start.index
                and (source == ".gitignore" or source.endswith("/.gitignore"))):
            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = worktree / source_path
            source_key = str(_canonical_path(source_path))
            proven = any(
                key.startswith('ignore-source:') and len(value) == 2
                and value[0] == source_key and value[1] not in {'missing', 'disabled'}
                and start.config.get(key) == saved.config.get(key) == value
                for key, value in current.config.items()
            )
            if proven:
                dirty.pop(path)
            continue
        if (source not in start.index or start.index.get(source) != saved.index.get(source)
                or saved.index.get(source) != current.index.get(source)):
            continue
        if source in start.dirty or source in saved.dirty or source in current.dirty:
            continue
        old_rule = effects.git(worktree, "rev-parse", f"{start.head}:{source}", check=False)
        new_rule = effects.git(worktree, "rev-parse", f"{current.head}:{source}", check=False)
        if old_rule.returncode or new_rule.returncode or old_rule.stdout != new_rule.stdout:
            continue
        dirty.pop(path)
    return replace(saved, dirty=dirty) if dirty != saved.dirty else saved


def capture_git_snapshot(worktree: Path, *, effects: ObservationEffects,
                         details: dict | None = None) -> GitSnapshot:
    """Capture local and repository-wide Git state without staging content."""

    with snapshot_timing(effects) as observed:
        # 상세 결과는 이 호출의 관측 안에서만 전달하고 저장 snapshot에는 넣지 않는다.
        if details is not None:
            details.clear()
        head, branch, index, dirty = _capture_local_worktree_state(
            worktree, effects=observed, **({"details": details} if details is not None else {}))
        paths = {} if details is not None else None
        operations = _git_operation_entries(worktree, effects=observed, paths=paths)
        if details is not None:
            details["git_paths"] = paths
        return GitSnapshot(
            head=head,
            branch=branch,
            index=index,
            dirty=dirty,
            refs=_shared_ref_entries(worktree, effects=observed),
            worktrees=_registered_worktree_entries(worktree, effects=observed),
            config=_config_entries(worktree, effects=observed),
            operations=operations,
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
