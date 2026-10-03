"""Manager-owned, source-bound Git context; never an execution-state backup."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat

from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError, _git


ARTIFACT_PATH = "docs/maintenance/code-map.jsonl"
VERSION = 1
MAX_ARTIFACT_BYTES = 32_000_000
_SCHEMA = """CREATE TABLE IF NOT EXISTS maintenance_code_map_exports (
    repo TEXT PRIMARY KEY,
    cycle_revision INTEGER NOT NULL,
    source_revision TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)"""


class CodeMapArtifactError(RuntimeError):
    """A Manager artifact cannot be written or matched to its durable receipt."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate code-map key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite code-map value")


def read_maps(repository: Path, inventory, known_maps=None) -> dict[str, dict]:
    """Read the exact Git artifact and ground new context against current blobs."""
    from loop_manager_internal.maintenance.maintenance_file_review import validate_code_map
    from loop_manager_internal.maintenance.file_review_protocol import FileReviewError

    if not inventory.sources or all(source.path in (known_maps or {}) for source in inventory.sources):
        return {}
    entries = _git(repository, "ls-tree", "-z", inventory.revision, "--", ARTIFACT_PATH)
    if not entries:
        return {}
    try:
        metadata, path = entries.rstrip(b"\0").split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        if path.decode() != ARTIFACT_PATH or mode != b"100644" or kind != b"blob":
            return {}
        if int(_git(repository, "cat-file", "-s", oid.decode())) > MAX_ARTIFACT_BYTES:
            return {}
        lines = io.BytesIO(_git(repository, "cat-file", "blob", oid.decode()))
        header = json.loads(lines.readline(), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if (not isinstance(header, dict) or set(header) != {"version"}
            or type(header["version"]) is not int or header["version"] != VERSION):
            return {}
        sources = {item.path: item for item in inventory.sources}
        result, seen = {}, set()
        for line in lines:
            entry = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if (not isinstance(entry, dict) or set(entry) != {"path", "content_hash", "mode", "code_map"}
                or not isinstance(entry["path"], str) or entry["path"] in seen):
                return {}
            seen.add(entry["path"])
            source = sources.get(entry["path"])
            if not source or (entry["content_hash"], entry["mode"]) != (source.content_hash, source.mode):
                continue
            if entry["path"] in (known_maps or {}):
                continue
            content = _git(repository, "show", f"{inventory.revision}:{source.path}")
            if hashlib.sha256(content).hexdigest() != source.content_hash:
                continue
            try:
                result[source.path] = validate_code_map(entry["code_map"], source, content)
            except FileReviewError:
                continue
        return result
    except SourceInventoryError:
        raise
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        # A malformed context artifact does not make the assessment successful
        # or block normal source review; verified Git-read failures still propagate.
        return {}


def serialize(inventory, maps: dict[str, dict]) -> bytes:
    """Stable bytes contain source context only, with no local state or secrets."""
    sources = {item.path: item for item in inventory.sources}
    records = [{"version": VERSION}]
    records.extend({"path": path, "content_hash": sources[path].content_hash,
                    "mode": sources[path].mode, "code_map": maps[path]}
                   for path in sorted(maps) if path in sources)
    return b"".join((json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                     + "\n").encode() for record in records)


def _safe_target(worktree: Path) -> Path:
    """Reject symlink traversal and special files before reading or writing."""
    current = worktree
    parts = Path(ARTIFACT_PATH).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or (index < len(parts) - 1 and not stat.S_ISDIR(mode)) \
                or (index == len(parts) - 1 and not stat.S_ISREG(mode)):
            raise CodeMapArtifactError("코드맵 출력 경로에 symlink 또는 특수 파일이 있습니다.")
    return current


def _receipt(connection: sqlite3.Connection, repo: str):
    connection.execute(_SCHEMA)
    row = connection.execute(
        "SELECT cycle_revision, source_revision, artifact_hash FROM maintenance_code_map_exports WHERE repo = ?",
        (repo,),
    ).fetchone()
    if not row:
        raise CodeMapArtifactError("검증된 Manager 코드맵 출력 기록이 없습니다.")
    return row


def restore_conflict(connection: sqlite3.Connection, repo: str, worktree: Path, stash_sha: str = ""):
    """Restore only receipt-matching Manager bytes; fresh assessment rebuilds them."""
    row = _receipt(connection, repo)
    target = _safe_target(worktree)
    try:
        if stash_sha:
            entries = _git(worktree, "ls-tree", "-z", f"{stash_sha}^3", "--", ARTIFACT_PATH)
            metadata, path = entries.rstrip(b"\0").split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
            if mode != b"100644" or kind != b"blob" or path.decode() != ARTIFACT_PATH:
                raise ValueError("Invalid code-map stash entry")
        else:
            entries = _git(worktree, "ls-files", "--stage", "-z", "--", ARTIFACT_PATH)
            candidates = []
            for entry in entries.rstrip(b"\0").split(b"\0"):
                metadata, path = entry.split(b"\t", 1)
                mode, oid, stage = metadata.split(b" ")
                if mode == b"100644" and stage == b"3" and path.decode() == ARTIFACT_PATH:
                    candidates.append(oid)
            if len(candidates) != 1:
                raise ValueError("Missing Manager side of code-map conflict")
            oid = candidates[0]
        if int(_git(worktree, "cat-file", "-s", oid.decode())) > MAX_ARTIFACT_BYTES:
            raise ValueError("Oversized code-map conflict")
        contents = _git(worktree, "cat-file", "blob", oid.decode())
        if hashlib.sha256(contents).hexdigest() != row[2]:
            raise ValueError("Conflict bytes do not match the Manager receipt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        target.chmod(0o644)
    except (ValueError, OSError) as error:
        raise CodeMapArtifactError("충돌한 코드맵의 Manager 원본을 확인할 수 없습니다.") from error


def attach_candidate(connection: sqlite3.Connection, context, worktree: Path, result, *, allow_stale=False):
    """Only a durable Manager receipt can add this path to a validated batch."""
    from loop_manager_internal.application.contracts import ValidatedPlanningResult

    row = _receipt(connection, context.repo)
    target = _safe_target(worktree)
    try:
        contents = target.read_bytes()
    except OSError as error:
        raise CodeMapArtifactError("Manager 코드맵 출력을 읽을 수 없습니다.") from error
    digest = hashlib.sha256(contents).hexdigest()
    if digest != row[2]:
        raise CodeMapArtifactError("코드맵 출력이 검증된 Manager 기록과 다릅니다.")
    if tuple(row[:2]) != (context.cycle_revision, context.active_sha):
        if allow_stale and context.cycle_phase == "assessment":
            # This is owned recovery output, never current publication evidence.
            # Publication preparation must collect the new source and rebuild it.
            return result
        raise CodeMapArtifactError("Manager 코드맵 출력의 원본 identity가 현재 평가와 다릅니다.")
    result = result or ValidatedPlanningResult((), (), maintenance_context=context)
    paths = tuple(sorted(set(result.publication_paths) | {ARTIFACT_PATH}))
    hashes = {**dict(result.publication_hashes), ARTIFACT_PATH: digest}
    return replace(result, publication_paths=paths, publication_hashes=tuple(sorted(hashes.items())),
                   maintenance_code_map_hash=digest)


def prepare(connection: sqlite3.Connection, repo: str, context, worktree: Path, evidence, result):
    """Freeze completed maps and save provenance before the first filesystem write."""
    from loop_manager_internal.maintenance.maintenance_reconciliation import ReconciliationRepository

    if (context is None or context.repo != repo or context.active_sha != evidence.inventory.revision
        or context.cycle_phase != "assessment"):
        raise CodeMapArtifactError("코드맵 출력과 활성 평가의 원본 identity가 다릅니다.")
    target = _safe_target(worktree)
    if evidence.tiered is not None:
        from loop_manager_internal.maintenance.code_index import CodeIndex
        contents = CodeIndex(evidence.inventory.revision, evidence.tiered["records"], 0, 0).serialize()
    else:
        maps = ReconciliationRepository(connection).valid_maps(repo, evidence.inventory)
        maps.update((path, json.loads(raw)) for path, raw in evidence.code_maps)
        contents = serialize(evidence.inventory, maps)
    if len(contents) > MAX_ARTIFACT_BYTES:
        raise CodeMapArtifactError("코드맵 출력이 허용 크기를 초과했습니다.")
    existing = target.read_bytes() if target.exists() else None
    # Already tracked identical output needs neither a new receipt nor a commit.
    base = _git(worktree, "ls-tree", "-z", context.active_sha, "--", ARTIFACT_PATH)
    baseline = _git(worktree, "show", f"{context.active_sha}:{ARTIFACT_PATH}") if base else None
    if existing == contents == baseline:
        return result
    connection.execute(_SCHEMA)
    with connection:
        connection.execute(
            "INSERT INTO maintenance_code_map_exports VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET cycle_revision = excluded.cycle_revision, "
            "source_revision = excluded.source_revision, artifact_hash = excluded.artifact_hash",
            (repo, context.cycle_revision, context.active_sha, hashlib.sha256(contents).hexdigest()),
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    target.chmod(0o644)
    if baseline == contents:
        return result
    return attach_candidate(connection, context, worktree, result)
