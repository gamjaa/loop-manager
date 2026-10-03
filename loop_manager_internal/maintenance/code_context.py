"""Read-only indexed source queries, isolated from the operational SQLite DB."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from loop_manager_internal.maintenance import code_index


def common_directory(repository):
    raw = Path(code_index.git(repository, "rev-parse", "--git-common-dir").decode().strip())
    return str((raw if raw.is_absolute() else repository / raw).resolve())


def export_snapshot(directory: Path, repository: Path, index: code_index.CodeIndex) -> Path:
    """Export facts only. Agents never open the Manager's operational database."""
    common = common_directory(repository)
    identity = hashlib.sha256((common + index.digest).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("code-context-" + identity + ".sqlite")
    if target.is_file() and not target.is_symlink():
        return target
    temporary = directory / (".code-context-" + uuid4().hex + ".sqlite")
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE records (path TEXT PRIMARY KEY, content_hash TEXT, mode TEXT, facts_json TEXT)")
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", [("kind", "static_code_context"),
                ("version", str(code_index.VERSION)), ("common_directory", common), ("source_revision", index.revision)])
            connection.executemany("INSERT INTO records VALUES (?, ?, ?, ?)", ((path, item["content_hash"], item["mode"],
                code_index.encode(item["facts"])) for path, item in sorted(index.records.items())))
        temporary.chmod(0o444)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def query_snapshot(repository: Path, requested: list[str], snapshot: Path) -> dict:
    """Reuse current facts; reparse changed bodies and refresh affected relations."""
    if not snapshot.is_file() or snapshot.is_symlink():
        raise ValueError("정적 코드맵 조회 snapshot이 일반 파일이 아닙니다.")
    tracked = set(code_index.git(repository, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    deleted = set(code_index.git(repository, "ls-files", "--deleted", "-z").decode().split("\0"))
    tracked -= deleted
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if (metadata.get("kind") != "static_code_context" or metadata.get("version") != str(code_index.VERSION)
                or metadata.get("common_directory") != common_directory(repository)):
            raise ValueError("코드맵 조회 snapshot의 저장소·분석 버전이 다릅니다.")
        identities = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT path, content_hash, mode FROM records")}
        changed = set(code_index.git(repository, "ls-files", "--modified", "--others", "--exclude-standard", "-z").decode().split("\0"))
        changed.update(code_index.git(repository, "diff", "--cached", "--name-only", "-z").decode().split("\0"))
        head = code_index.git(repository, "rev-parse", "HEAD").decode().strip()
        if head != metadata["source_revision"]:
            changed.update(code_index.git(repository, "diff", "--name-only", "-z", metadata["source_revision"], head).decode().split("\0"))
        raw, current, loaded, parsed = {}, {}, {}, []
        def saved(path):
            if path not in loaded:
                row = connection.execute("SELECT facts_json FROM records WHERE path = ?", (path,)).fetchone()
                loaded[path] = json.loads(row[0]) if row else None
            return loaded[path]
        candidates = set(requested)
        for path in requested:
            previous = saved(path)
            if previous:
                candidates.update(previous["dependents"])
        candidates.update(path for path in changed if path in identities or (
            path.endswith((".py", ".pyi")) and path.split("/", 1)[0] not in {"docs", ".agents", ".codex"}))
        def load(path):
            content, mode = code_index.read_worktree_source(repository, path, tracked)
            content_hash = hashlib.sha256(content).hexdigest()
            previous = saved(path)
            if previous is not None and identities.get(path) == (content_hash, mode):
                fact = previous
            else:
                fact = code_index.parse_source(path, content)
                parsed.append(path)
            raw[path] = fact
            current[path] = {"content_hash": content_hash, "mode": mode}
        for path in sorted(candidates & tracked):
            load(path)
        for path in requested:
            if path not in raw:
                raise ValueError("코드맵 조회 대상이 현재 worktree에 없습니다: " + path)
        resolved = code_index.resolve(raw, tracked)
        targets = {reference["path"] for path in requested for reference in resolved[path]["references"]}
        for path in sorted((targets & tracked) - raw.keys()):
            load(path)
        resolved = code_index.resolve(raw, tracked)
        return {"kind": "static", "analysis_version": code_index.VERSION, "worktree": str(repository.resolve()),
            "incoming_coverage": "snapshot_and_current_changes", "parsed_paths": parsed,
            "records": {path: {**current[path], "facts": resolved[path]} for path in requested}}
