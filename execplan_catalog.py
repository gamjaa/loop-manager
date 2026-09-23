"""Pure ExecPlan parsing and active/historical catalog snapshots.

The filesystem location of a plan is deliberately not an execution state.  A
snapshot exposes top-level active plans separately from top-level archived
plans while validating identity and dependencies across both collections.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import AbstractSet, Iterable, Mapping


PRIORITIES = frozenset({"critical", "high", "normal", "low"})
_PLAN_ID = re.compile(r"G-\d{3,}")
_ACTIVE_PATH = re.compile(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md")
_ARCHIVED_PATH = re.compile(
    r"docs/plans/archive/\[(G-\d{3,})\] [^/]+\.md"
)
_ACTIVE_REQUIREMENT_PATH = re.compile(r"docs/requirements/[^/]+\.md")


def plan_content_hash(content: bytes | str) -> str:
    """Hash the exact UTF-8 bytes of a validated ExecPlan document."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def final_content_matches(observed_hash: str, current_hash: str) -> bool:
    """Check final bytes independently of immutable publication audit bytes."""
    return bool(current_hash) and observed_hash == current_hash


def conflicting_archived_plan_paths(
    paths: Iterable[str],
) -> Mapping[str, tuple[str, ...]]:
    """Find archived paths sharing an ID with an active plan in one tree."""
    paths = tuple(paths)
    active_ids = {
        match.group(1)
        for path in paths
        if (match := _ACTIVE_PATH.fullmatch(path)) is not None
    }
    conflicts: dict[str, list[str]] = {}
    for path in paths:
        match = _ARCHIVED_PATH.fullmatch(path)
        if match is not None and match.group(1) in active_ids:
            conflicts.setdefault(match.group(1), []).append(path)
    return {
        plan_id: tuple(sorted(conflicts[plan_id]))
        for plan_id in sorted(conflicts)
    }


class ArchiveDocumentKind(str, Enum):
    """Document families whose byte-preserving moves are lifecycle-neutral."""

    REQUIREMENT = "requirement"
    EXECPLAN = "execplan"


@dataclass(frozen=True)
class CatalogArchiveMove:
    kind: ArchiveDocumentKind
    source: str
    destination: str


@dataclass(frozen=True)
class ArchiveOnlyChange:
    """Classification of a whole-tree change as content-preserving moves."""

    moves: tuple[CatalogArchiveMove, ...] = ()

    @property
    def archive_only(self) -> bool:
        return bool(self.moves)

    @property
    def requirement_only(self) -> bool:
        return self.archive_only and all(
            move.kind is ArchiveDocumentKind.REQUIREMENT for move in self.moves
        )

    @property
    def execplan_only(self) -> bool:
        return self.archive_only and all(
            move.kind is ArchiveDocumentKind.EXECPLAN for move in self.moves
        )


def _archive_destination(path: str) -> tuple[ArchiveDocumentKind, str] | None:
    if _ACTIVE_REQUIREMENT_PATH.fullmatch(path):
        filename = path.removeprefix("docs/requirements/")
        return (
            ArchiveDocumentKind.REQUIREMENT,
            f"docs/requirements/archive/{filename}",
        )
    if _ACTIVE_PATH.fullmatch(path):
        filename = path.removeprefix("docs/plans/")
        return (
            ArchiveDocumentKind.EXECPLAN,
            f"docs/plans/archive/{filename}",
        )
    return None


def classify_archive_only_tree_change(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> ArchiveOnlyChange:
    """Recognize only exact active-to-archive moves with identical tree entries.

    Values are opaque but must compare equal for byte/mode identity.  Callers
    can therefore supply Git ``(mode, type, object-id)`` tuples or test tokens
    without coupling this pure catalog rule to Git subprocesses.
    """

    removed = set(previous) - set(current)
    added = set(current) - set(previous)
    changed = {
        path
        for path in set(previous) & set(current)
        if previous[path] != current[path]
    }
    if not removed or changed:
        return ArchiveOnlyChange()

    moves: list[CatalogArchiveMove] = []
    expected_added: set[str] = set()
    for source in sorted(removed):
        target = _archive_destination(source)
        if target is None:
            return ArchiveOnlyChange()
        kind, destination = target
        if (
            destination in previous
            or destination not in added
            or current[destination] != previous[source]
        ):
            return ArchiveOnlyChange()
        expected_added.add(destination)
        moves.append(CatalogArchiveMove(kind, source, destination))

    if added != expected_added:
        return ArchiveOnlyChange()
    return ArchiveOnlyChange(tuple(moves))


class ExecPlanCatalogError(RuntimeError):
    """A catalog validation error tied to the remote file that caused it."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: str | None = None,
        source: str | None = None,
        related_paths: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.code = code
        self.path = path
        self.source = source
        self.related_paths = tuple(
            dict.fromkeys(
                item
                for item in (path, source, *related_paths)
                if item is not None
            )
        )


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the deliberately small frontmatter subset used by ExecPlans."""

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
            value = values[current_list]
            assert isinstance(value, list)
            value.append(line[4:].strip())
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


def requirement_archive_path(source: str) -> str:
    match = re.fullmatch(r"docs/requirements/([^/\\]+\.md)", source)
    if match is None:
        raise ValueError(f"활성 Requirement 경로가 아닙니다: {source}")
    return f"docs/requirements/archive/{match.group(1)}"


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _source_exists(
    root: Path,
    source: str,
    *,
    allow_archive_fallback: bool,
) -> bool:
    if _is_regular_file(root / source):
        return True
    return bool(
        allow_archive_fallback
        and source != "docs/PRODUCT.md"
        and _is_regular_file(root / requirement_archive_path(source))
    )


def parse_execplan_text(
    relative_path: str,
    text: str,
    *,
    root: Path | None = None,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, object]:
    """Parse one plan without consulting Git, SQLite, or external services."""

    legacy = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    allowed = legacy | {"priority"}
    values = parse_frontmatter(text)
    if set(values) not in {frozenset(legacy), frozenset(allowed)}:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
    if "priority" not in values:
        if require_priority:
            raise RuntimeError("새 ExecPlan에는 priority가 필요합니다.")
        values["priority"] = "normal"

    plan_id = values["id"]
    title = values["title"]
    filename = PurePosixPath(relative_path).name
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        raise RuntimeError("ExecPlan id는 G-000 형식이어야 합니다.")
    if not isinstance(title, str) or filename != f"[{plan_id}] {title}.md":
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
        if (
            validate_sources
            and root is not None
            and not _source_exists(
                root,
                source,
                allow_archive_fallback=allow_archive_fallback,
            )
        ):
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    for dependency in dependencies:
        if not isinstance(dependency, str) or _PLAN_ID.fullmatch(dependency) is None:
            raise RuntimeError(
                f"ExecPlan depends_on ID 형식이 유효하지 않습니다: {dependency}"
            )
    if values["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("ExecPlan complexity가 유효하지 않습니다.")
    if values["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("ExecPlan risk가 유효하지 않습니다.")
    if values["priority"] not in PRIORITIES:
        raise RuntimeError("ExecPlan priority가 유효하지 않습니다.")
    return values


@dataclass(frozen=True)
class ExecPlanCatalogEntry:
    """One immutable file identity observed in a catalog snapshot."""

    plan_id: str
    path: str
    active_path: str
    title: str
    sources: tuple[str, ...]
    depends_on: tuple[str, ...]
    complexity: str
    risk: str
    priority: str
    content_hash: str
    text: str
    archived: bool

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.plan_id,
            "title": self.title,
            "sources": list(self.sources),
            "depends_on": list(self.depends_on),
            "complexity": self.complexity,
            "risk": self.risk,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class ExecPlanDefinition:
    """One validated definition pinned to an immutable base revision."""

    entry: ExecPlanCatalogEntry
    revision_sha: str

    @property
    def plan_id(self) -> str:
        return self.entry.plan_id

    @property
    def path(self) -> str:
        return self.entry.path

    @property
    def title(self) -> str:
        return self.entry.title

    @property
    def sources(self) -> tuple[str, ...]:
        return self.entry.sources

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.entry.depends_on


@dataclass(frozen=True)
class ExecPlanCatalogSnapshot:
    """A full identity graph with separate executable and historical views."""

    active: Mapping[str, ExecPlanCatalogEntry]
    archived: Mapping[str, ExecPlanCatalogEntry]
    revision_sha: str | None = None

    @property
    def historical(self) -> Mapping[str, ExecPlanCatalogEntry]:
        return self.archived

    @property
    def all(self) -> Mapping[str, ExecPlanCatalogEntry]:
        combined = {**self.active, **self.archived}
        return MappingProxyType(
            dict(sorted(combined.items(), key=lambda item: int(item[0][2:])))
        )

    @property
    def next_plan_number(self) -> int:
        return max((int(plan_id[2:]) for plan_id in self.all), default=0) + 1

    @property
    def next_plan_id(self) -> str:
        return f"G-{self.next_plan_number:03d}"

    def active_metadata(self) -> dict[str, dict[str, object]]:
        return {
            plan_id: entry.metadata() for plan_id, entry in self.active.items()
        }

    def definitions(self) -> Mapping[str, ExecPlanDefinition]:
        if self.revision_sha is None:
            raise RuntimeError("ExecPlan catalog snapshot에 revision SHA가 없습니다.")
        return MappingProxyType(
            {
                plan_id: ExecPlanDefinition(entry, self.revision_sha)
                for plan_id, entry in self.all.items()
            }
        )


def _entry(relative_path: str, text: str, values: Mapping[str, object]) -> ExecPlanCatalogEntry:
    archived = _ARCHIVED_PATH.fullmatch(relative_path) is not None
    plan_id = str(values["id"])
    title = str(values["title"])
    return ExecPlanCatalogEntry(
        plan_id=plan_id,
        path=relative_path,
        active_path=f"docs/plans/[{plan_id}] {title}.md",
        title=title,
        sources=tuple(str(item) for item in values["sources"]),
        depends_on=tuple(str(item) for item in values["depends_on"]),
        complexity=str(values["complexity"]),
        risk=str(values["risk"]),
        priority=str(values["priority"]),
        content_hash=plan_content_hash(text),
        text=text,
        archived=archived,
    )


def _validate_full_graph(entries: Mapping[str, ExecPlanCatalogEntry]) -> None:
    for plan_id, entry in entries.items():
        missing = sorted(
            dependency for dependency in entry.depends_on if dependency not in entries
        )
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
        for dependency in sorted(entries[plan_id].depends_on):
            if states.get(dependency) == "visiting":
                start = stack.index(dependency)
                raise RuntimeError(
                    "순환 ExecPlan 의존성입니다: "
                    + " -> ".join([*stack[start:], dependency])
                )
            if states.get(dependency) is None:
                visit(dependency)
        stack.pop()
        states[plan_id] = "visited"

    for plan_id in entries:
        if states.get(plan_id) is None:
            visit(plan_id)


def snapshot_from_documents(
    documents: Mapping[str, str | bytes],
    *,
    root: Path | None = None,
    validate_sources: bool = False,
    allow_archive_fallback: bool = True,
    available_regular_paths: AbstractSet[str] | None = None,
    invalid_plan_entries: Mapping[str, str] | None = None,
    revision_sha: str | None = None,
    archive_conflict_plan_ids: AbstractSet[str] = frozenset(),
) -> ExecPlanCatalogSnapshot:
    """Build a snapshot from already-read regular-file documents.

    Callers that read Git trees can use this API without making the parser
    aware of Git.  Nested paths are ignored by the exact path expressions.
    """

    if invalid_plan_entries:
        relative_path = sorted(invalid_plan_entries)[0]
        kind = invalid_plan_entries[relative_path]
        raise ExecPlanCatalogError(
            f"ExecPlan은 원격 revision의 일반 파일이어야 합니다: "
            f"{relative_path} ({kind})",
            code="non-regular-plan",
            path=relative_path,
        )

    active: dict[str, ExecPlanCatalogEntry] = {}
    archived: dict[str, ExecPlanCatalogEntry] = {}
    filenames: dict[str, str] = {}
    for relative_path in sorted(documents):
        active_candidate = re.fullmatch(
            r"docs/plans/[^/]+\.md", relative_path
        )
        archived_candidate = re.fullmatch(
            r"docs/plans/archive/[^/]+\.md", relative_path
        )
        if active_candidate is None and archived_candidate is None:
            continue
        raw = documents[relative_path]
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError as error:
            raise ExecPlanCatalogError(
                f"ExecPlan 파일이 UTF-8이 아닙니다: {relative_path}",
                code="invalid-encoding",
                path=relative_path,
            ) from error
        try:
            values = parse_execplan_text(
                relative_path,
                text,
                root=root,
                validate_sources=(
                    validate_sources and available_regular_paths is None
                ),
                allow_archive_fallback=allow_archive_fallback,
            )
        except RuntimeError as error:
            raise ExecPlanCatalogError(
                f"ExecPlan 파일이 유효하지 않습니다: {relative_path} ({error})",
                code="invalid-plan",
                path=relative_path,
            ) from error
        entry = _entry(relative_path, text, values)
        if validate_sources and available_regular_paths is not None:
            for source in entry.sources:
                exists = source in available_regular_paths
                archived_source = None
                if source != "docs/PRODUCT.md":
                    archived_source = requirement_archive_path(source)
                    exists = exists or bool(
                        allow_archive_fallback
                        and archived_source in available_regular_paths
                    )
                if not exists:
                    raise ExecPlanCatalogError(
                        "존재하지 않는 ExecPlan source입니다: "
                        f"{source} ({relative_path})",
                        code="missing-source",
                        path=relative_path,
                        source=source,
                        related_paths=(
                            (archived_source,) if archived_source else ()
                        ),
                    )
        all_entries = {**active, **archived}
        if entry.plan_id in all_entries:
            other = all_entries[entry.plan_id]
            if (
                entry.archived
                and not other.archived
                and entry.plan_id in archive_conflict_plan_ids
            ):
                # Only a validated copy of a SQLite-confirmed MERGED plan
                # may be handled as an archive candidate path conflict.
                continue
            raise ExecPlanCatalogError(
                f"중복 ExecPlan id입니다: {entry.plan_id} "
                f"({other.path}, {relative_path})",
                code="duplicate-id",
                path=relative_path,
                related_paths=(other.path,),
            )
        filename = PurePosixPath(relative_path).name
        if filename in filenames:
            raise ExecPlanCatalogError(
                "중복 ExecPlan 파일명입니다: "
                f"{filename} ({filenames[filename]}, {relative_path})",
                code="duplicate-path",
                path=relative_path,
                related_paths=(filenames[filename],),
            )
        filenames[filename] = relative_path
        (archived if entry.archived else active)[entry.plan_id] = entry

    combined = {**active, **archived}
    combined = dict(sorted(combined.items(), key=lambda item: int(item[0][2:])))
    try:
        _validate_full_graph(combined)
    except RuntimeError as error:
        message = str(error)
        mentioned = tuple(
            entry.path
            for plan_id, entry in combined.items()
            if plan_id in message
        )
        raise ExecPlanCatalogError(
            message,
            code=(
                "dependency-cycle"
                if "순환" in message
                else "missing-dependency"
            ),
            path=(mentioned[0] if mentioned else None),
            related_paths=mentioned[1:],
        ) from error
    sort_key = lambda item: int(item[0][2:])
    return ExecPlanCatalogSnapshot(
        active=MappingProxyType(dict(sorted(active.items(), key=sort_key))),
        archived=MappingProxyType(dict(sorted(archived.items(), key=sort_key))),
        revision_sha=revision_sha,
    )


def _top_level_regular_documents(directory: Path, relative_root: str) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    try:
        children = list(directory.iterdir())
    except FileNotFoundError:
        return documents
    for path in sorted(children, key=lambda item: item.name):
        if path.suffix != ".md" or not _is_regular_file(path):
            continue
        documents[f"{relative_root}/{path.name}"] = path.read_bytes()
    return documents


def load_execplan_snapshot(
    root: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> ExecPlanCatalogSnapshot:
    """Read only direct regular files from active and archive directories."""

    documents = _top_level_regular_documents(root / "docs/plans", "docs/plans")
    documents.update(
        _top_level_regular_documents(
            root / "docs/plans/archive", "docs/plans/archive"
        )
    )
    return snapshot_from_documents(
        documents,
        root=root,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )


def parse_execplan_file(
    root: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, object]:
    """Compatibility parser for one explicitly selected regular file."""

    if not _is_regular_file(path):
        raise RuntimeError(f"ExecPlan은 일반 파일이어야 합니다: {path}")
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = path.as_posix()
    return parse_execplan_text(
        relative_path,
        path.read_text(encoding="utf-8"),
        root=root,
        require_priority=require_priority,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )


def validate_dependency_graph(catalog: Mapping[str, Mapping[str, object]]) -> None:
    """Compatibility validator for callers still holding metadata dictionaries."""

    entries: dict[str, ExecPlanCatalogEntry] = {}
    for plan_id, plan in catalog.items():
        entries[plan_id] = ExecPlanCatalogEntry(
            plan_id=plan_id,
            path=f"docs/plans/[{plan_id}] {plan.get('title', '')}.md",
            active_path=f"docs/plans/[{plan_id}] {plan.get('title', '')}.md",
            title=str(plan.get("title", "")),
            sources=tuple(str(item) for item in plan.get("sources", [])),
            depends_on=tuple(str(item) for item in plan.get("depends_on", [])),
            complexity=str(plan.get("complexity", "low")),
            risk=str(plan.get("risk", "low")),
            priority=str(plan.get("priority", "normal")),
            content_hash="",
            text="",
            archived=False,
        )
    _validate_full_graph(entries)
