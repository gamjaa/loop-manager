"""Pure ExecPlan parsing and active/historical catalog snapshots.

The filesystem location of a plan is deliberately not an execution state.  A
snapshot exposes top-level active plans separately from top-level archived
plans while validating identity and dependencies across both collections.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import AbstractSet, Iterable, Mapping
from loop_manager_internal.catalog import execplan_drafts, manager_sources
from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.catalog.frontmatter import parse_frontmatter as _parse_shared_frontmatter


PRIORITIES = frozenset({"critical", "high", "normal", "low"})
_PLAN_ID = re.compile(r"G-\d{3,}")
_ACTIVE_PATH = re.compile(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md")
_ARCHIVED_PATH = re.compile(
    r"docs/plans/archive/\[(G-\d{3,})\] [^/]+\.md"
)
_ACTIVE_REQUIREMENT_PATH = re.compile(r"docs/requirements/[^/]+\.md")


def is_active_execplan_path(path: str | bytes) -> bool:
    if isinstance(path, bytes):
        path = path.decode("utf-8", "surrogateescape")
    return _ACTIVE_PATH.fullmatch(path) is not None


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
    values = _parse_shared_frontmatter(text)
    if values is None:
        raise RuntimeError("ExecPlan frontmatter가 없습니다.")
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
    root: Path | None,
    source: str,
    *,
    allow_archive_fallback: bool,
    available_regular_paths: AbstractSet[str] | None = None,
    requirement_documents: Mapping[str, str | bytes] | None = None,
) -> bool:
    resolution = _source_lookup(
        root, source, allow_archive_fallback=allow_archive_fallback,
        available_regular_paths=available_regular_paths,
        requirement_documents=requirement_documents,
    )
    return resolution is None or resolution.status is requirement_sources.ResolutionStatus.RESOLVED


def _source_lookup(
    root: Path | None,
    source: str,
    *,
    allow_archive_fallback: bool,
    available_regular_paths: AbstractSet[str] | None = None,
    requirement_documents: Mapping[str, str | bytes] | None = None,
) -> requirement_sources.RequirementSourceResolution | None:
    if source == manager_sources.SOURCE:
        return None  # Registration is checked at Manager state/publication gates.
    if source == "docs/PRODUCT.md":
        if available_regular_paths is not None:
            return requirement_sources.RequirementSourceResolution(
                requirement_sources.ResolutionStatus.RESOLVED if source in available_regular_paths
                else requirement_sources.ResolutionStatus.MISSING, source,
                source if source in available_regular_paths else None,
            )
        if root is not None:
            exists = _is_regular_file(root / source)
            return requirement_sources.RequirementSourceResolution(
                requirement_sources.ResolutionStatus.RESOLVED if exists
                else requirement_sources.ResolutionStatus.MISSING,
                source, source if exists else None,
            )
        return None
    if requirement_documents is None and root is not None:
        requirement_documents = requirement_sources.worktree_requirement_documents(root)
    if requirement_documents is not None:
        return requirement_sources.resolve_requirement_source(
            source, requirement_documents, allow_archive=allow_archive_fallback,
        )
    if available_regular_paths is not None:
        if source in available_regular_paths:
            status, path = requirement_sources.ResolutionStatus.RESOLVED, source
        elif allow_archive_fallback and requirement_archive_path(source) in available_regular_paths:
            status, path = requirement_sources.ResolutionStatus.RESOLVED, requirement_archive_path(source)
        else:
            status, path = requirement_sources.ResolutionStatus.MISSING, None
        return requirement_sources.RequirementSourceResolution(status, source, path)
    if root is None:
        return None
    # The Requirement tree was read above; this branch is only reachable for
    # a non-Requirement source path that cannot use the shared resolver.
    exists = _is_regular_file(root / source)
    return requirement_sources.RequirementSourceResolution(
        requirement_sources.ResolutionStatus.RESOLVED if exists
        else requirement_sources.ResolutionStatus.MISSING,
        source, source if exists else None,
    )


def parse_execplan_text(
    relative_path: str,
    text: str,
    *,
    root: Path | None = None,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
    requirement_documents: Mapping[str, str | bytes] | None = None,
) -> dict[str, object]:
    """Parse one plan without consulting Git, SQLite, or external services."""

    legacy = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    allowed = legacy | {"priority", "draft"}
    values = parse_frontmatter(text)
    if not legacy <= set(values) <= allowed:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
    if "draft" in values:
        if not isinstance(values["draft"], str) or values["draft"] not in {"true", "false"}:
            raise RuntimeError("ExecPlan draft는 true 또는 false여야 합니다.")
        values["draft"] = values["draft"] == "true"
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
            source == manager_sources.SOURCE
            or source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"허용되지 않는 ExecPlan source입니다: {source}")
        if validate_sources and (root is not None or requirement_documents is not None):
            resolution = _source_lookup(
                root, source, allow_archive_fallback=allow_archive_fallback,
                requirement_documents=requirement_documents,
            )
            if resolution is None or resolution.status is requirement_sources.ResolutionStatus.RESOLVED:
                continue
            if resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError(
                    f"ExecPlan source가 모호합니다: {source} ({', '.join(resolution.candidates)})"
                )
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    if manager_sources.SOURCE in sources:
        manager_sources.validate_scope(text, sources)
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
    values.update(execplan_drafts.metadata(text, values))
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
    draft_metadata: Mapping[str, object] | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.plan_id,
            "title": self.title,
            "sources": list(self.sources),
            "depends_on": list(self.depends_on),
            "complexity": self.complexity,
            "risk": self.risk,
            "priority": self.priority,
            **(self.draft_metadata or {}),
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
class CatalogIssue:
    """A declaration failure with identity proven by strict frontmatter parsing."""

    plan_id: str
    path: str
    code: str
    message: str
    source: str | None = None


@dataclass(frozen=True)
class ExecPlanCatalogSnapshot:
    """Validated declarations and, when isolated, revision-bound withheld identities."""

    active: Mapping[str, ExecPlanCatalogEntry]
    archived: Mapping[str, ExecPlanCatalogEntry]
    revision_sha: str | None = None
    isolation_enabled: bool = False
    issues: tuple[CatalogIssue, ...] = ()
    withheld: tuple[tuple[str, str, str], ...] = ()

    @property
    def observed_ids(self) -> frozenset[str]:
        return frozenset(self.all) | frozenset(item[0] for item in self.withheld)

    @property
    def withheld_ids(self) -> frozenset[str]:
        return frozenset(item[0] for item in self.withheld)

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
        return max((int(plan_id[2:]) for plan_id in self.observed_ids), default=0) + 1

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
        draft_metadata={key: value for key, value in values.items() if key.startswith("draft")},
    )


def _graph_issues(entries: Mapping[str, ExecPlanCatalogEntry],
                  observed_ids: AbstractSet[str]) -> tuple[CatalogIssue, ...]:
    issues = []
    for plan_id, entry in entries.items():
        missing = sorted(set(entry.depends_on) - observed_ids)
        if missing:
            issues.append(CatalogIssue(plan_id, entry.path, "missing-dependency",
                f"ExecPlan {plan_id}의 존재하지 않는 depends_on ID입니다: " + ", ".join(missing)))
    visited: set[str] = set()
    stack: list[str] = []

    def visit(plan_id: str) -> None:
        if plan_id in stack:
            cycle = stack[stack.index(plan_id):]
            message = "순환 ExecPlan 의존성입니다: " + " -> ".join([*cycle, plan_id])
            issues.extend(CatalogIssue(member, entries[member].path, "dependency-cycle", message)
                          for member in cycle)
            return
        if plan_id in visited or plan_id not in entries:
            return
        stack.append(plan_id)
        for dependency in sorted(entries[plan_id].depends_on):
            visit(dependency)
        stack.pop()
        visited.add(plan_id)

    for plan_id in entries:
        visit(plan_id)
    return tuple(issues)


def _validate_full_graph(entries: Mapping[str, ExecPlanCatalogEntry]) -> None:
    issues = _graph_issues(entries, frozenset(entries))
    if issues:
        raise RuntimeError(issues[0].message)


def snapshot_from_documents(
    documents: Mapping[str, str | bytes],
    *,
    root: Path | None = None,
    validate_sources: bool = False,
    allow_archive_fallback: bool = True,
    available_regular_paths: AbstractSet[str] | None = None,
    requirement_documents: Mapping[str, str | bytes] | None = None,
    invalid_plan_entries: Mapping[str, str] | None = None,
    revision_sha: str | None = None,
    archive_conflict_plan_ids: AbstractSet[str] = frozenset(),
    isolate_errors: bool = False,
) -> ExecPlanCatalogSnapshot:
    """Validate the full catalog, or explicitly derive an independent execution view.

    Broken frontmatter, encoding and non-regular files cannot prove identity.
    They always fail closed. Isolated errors never become executable entries.
    """
    if invalid_plan_entries:
        path = sorted(invalid_plan_entries)[0]
        raise ExecPlanCatalogError(
            f"ExecPlan은 원격 revision의 일반 파일이어야 합니다: "
            f"{path} ({invalid_plan_entries[path]})",
            code="non-regular-plan", path=path,
        )
    if validate_sources and requirement_documents is None and root is not None:
        requirement_documents = requirement_sources.worktree_requirement_documents(root)
    entries: dict[str, ExecPlanCatalogEntry] = {}
    identities: dict[str, tuple[str, str]] = {}
    issues: list[CatalogIssue] = []

    def reject(plan_id, path, code, message, source=None):
        if not isolate_errors:
            raise ExecPlanCatalogError(message, code=code, path=path, source=source)
        issues.append(CatalogIssue(plan_id, path, code, message, source))

    for path in sorted(documents):
        if not re.fullmatch(r"docs/plans/(?:archive/)?[^/]+\.md", path):
            continue
        raw = documents[path]
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError as error:
            raise ExecPlanCatalogError(
                f"ExecPlan 파일이 UTF-8이 아닙니다: {path}",
                code="invalid-encoding", path=path,
            ) from error
        # Identity is never guessed from a filename or a fragment of broken YAML.
        try:
            header = parse_frontmatter(text)
            plan_id, title = header.get("id"), header.get("title")
            if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
                raise RuntimeError("ExecPlan id는 G-000 형식이어야 합니다.")
            if (not isinstance(title, str)
                    or PurePosixPath(path).name != f"[{plan_id}] {title}.md"):
                raise RuntimeError("ExecPlan 파일명은 `[id] title.md`와 일치해야 합니다.")
        except RuntimeError as error:
            raise ExecPlanCatalogError(
                f"ExecPlan 파일이 유효하지 않습니다: {path} ({error})",
                code="invalid-plan", path=path,
            ) from error
        if plan_id in identities:
            other_path = identities[plan_id][0]
            # Preserve the existing, merge-proven archive conflict exception.
            if (path.startswith("docs/plans/archive/")
                    and not other_path.startswith("docs/plans/archive/")
                    and plan_id in archive_conflict_plan_ids
                    and plan_id in entries):
                try:
                    values = parse_execplan_text(path, text, validate_sources=False)
                except RuntimeError as error:
                    raise ExecPlanCatalogError(
                        f"ExecPlan 파일이 유효하지 않습니다: {path} ({error})",
                        code="invalid-plan", path=path,
                    ) from error
                if validate_sources:
                    for source in values["sources"]:
                        try:
                            resolution = _source_lookup(
                                root, source,
                                allow_archive_fallback=allow_archive_fallback,
                                available_regular_paths=available_regular_paths,
                                requirement_documents=requirement_documents,
                            )
                        except requirement_sources.RequirementSourceLookupError as error:
                            raise ExecPlanCatalogError(
                                str(error), code="source-lookup-failed",
                                path=path, source=source,
                            ) from error
                        if resolution is None or resolution.status is requirement_sources.ResolutionStatus.RESOLVED:
                            continue
                        if resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                            raise ExecPlanCatalogError(
                                f"ExecPlan source가 모호합니다: {source} "
                                f"({', '.join(resolution.candidates)})",
                                code="ambiguous-source", path=path, source=source,
                            )
                        raise ExecPlanCatalogError(
                            f"존재하지 않는 ExecPlan source입니다: {source} ({path})",
                            code="missing-source", path=path, source=source,
                        )
                continue
            raise ExecPlanCatalogError(
                f"중복 ExecPlan id입니다: {plan_id} ({other_path}, {path})",
                code="duplicate-id", path=path, related_paths=(other_path,),
            )
        identities[plan_id] = (path, plan_content_hash(text))
        try:
            values = parse_execplan_text(path, text, validate_sources=False)
        except RuntimeError as error:
            reject(plan_id, path, "invalid-plan",
                   f"ExecPlan 파일이 유효하지 않습니다: {path} ({error})")
            continue
        entry = _entry(path, text, values)
        entries[plan_id] = entry
        if validate_sources:
            for source in entry.sources:
                try:
                    resolution = _source_lookup(
                        root, source, allow_archive_fallback=allow_archive_fallback,
                        available_regular_paths=available_regular_paths,
                        requirement_documents=requirement_documents,
                    )
                except requirement_sources.RequirementSourceLookupError as error:
                    reject(plan_id, path, "source-lookup-failed",
                           str(error), source)
                    continue
                if resolution is None or resolution.status is requirement_sources.ResolutionStatus.RESOLVED:
                    continue
                if resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                    reject(plan_id, path, "ambiguous-source",
                           f"ExecPlan source가 모호합니다: {source} ({', '.join(resolution.candidates)})",
                           source)
                else:
                    reject(plan_id, path, "missing-source",
                           f"존재하지 않는 ExecPlan source입니다: {source} ({path})", source)

    manager_requests = {}
    for entry in entries.values():
        if not entry.archived and manager_sources.SOURCE in entry.sources:
            request_id = manager_sources.declaration(entry.text)["id"]
            if request_id in manager_requests:
                reject(entry.plan_id, entry.path, "duplicate-manager-request",
                       "동일 Manager 요청의 생성 계획이 중복됐습니다.")
            manager_requests[request_id] = entry.plan_id
    for issue in _graph_issues(entries, frozenset(identities)):
        reject(issue.plan_id, issue.path, issue.code, issue.message)
    ordered = dict(sorted(entries.items(), key=lambda item: int(item[0][2:])))
    snapshot = ExecPlanCatalogSnapshot(
        active=MappingProxyType({key: entry for key, entry in ordered.items() if not entry.archived}),
        archived=MappingProxyType({key: entry for key, entry in ordered.items() if entry.archived}),
        revision_sha=revision_sha, isolation_enabled=isolate_errors,
        withheld=tuple((key, *identities[key]) for key in sorted(identities.keys() - entries.keys())),
    )
    return withhold_catalog_issues(snapshot, tuple(issues))


def withhold_catalog_issues(snapshot: ExecPlanCatalogSnapshot,
                            issues: tuple[CatalogIssue, ...]) -> ExecPlanCatalogSnapshot:
    """Add identity/state validation failures to the same dependency closure."""
    entries = snapshot.all
    affected = set(snapshot.withheld_ids) | {issue.plan_id for issue in issues}
    dependents: dict[str, set[str]] = {}
    for key, entry in entries.items():
        for dependency in entry.depends_on:
            dependents.setdefault(dependency, set()).add(key)
    pending = list(affected)
    while pending:
        for key in dependents.get(pending.pop(), ()):
            if key not in affected:
                affected.add(key)
                pending.append(key)
    withheld = {key: (path, content_hash) for key, path, content_hash in snapshot.withheld}
    withheld.update({key: (entry.path, entry.content_hash) for key, entry in entries.items()
                     if key in affected})
    return replace(snapshot,
        active=MappingProxyType({key: entry for key, entry in snapshot.active.items() if key not in affected}),
        archived=MappingProxyType({key: entry for key, entry in snapshot.archived.items() if key not in affected}),
        issues=(*snapshot.issues, *issues),
        withheld=tuple((key, *withheld[key]) for key in sorted(withheld)),
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
    isolate_errors: bool = False,
) -> ExecPlanCatalogSnapshot:
    """Read only direct regular files from active and archive directories."""

    documents = _top_level_regular_documents(root / "docs/plans", "docs/plans")
    documents.update(
        _top_level_regular_documents(
            root / "docs/plans/archive", "docs/plans/archive"
        )
    )
    invalid = {}
    if isolate_errors:
        for directory in (root / "docs/plans", root / "docs/plans/archive"):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.suffix == ".md" and not _is_regular_file(path):
                    invalid[path.relative_to(root).as_posix()] = "non-regular"
    requirement_documents = (
        requirement_sources.worktree_requirement_documents(root)
        if validate_sources else None
    )
    return snapshot_from_documents(
        documents,
        invalid_plan_entries=invalid,
        root=root,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
        requirement_documents=requirement_documents,
        isolate_errors=isolate_errors,
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
