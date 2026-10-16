"""Resolve legacy ExecPlan Requirement source paths after Requirement renames."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import stat
from typing import Mapping

from loop_manager_internal.catalog.frontmatter import RequirementIdError, requirement_identity


_ACTIVE = re.compile(r"docs/requirements/[^/\\]+\.md\Z")
_ARCHIVED = re.compile(r"docs/requirements/archive/[^/\\]+\.md\Z")
_SOURCE_ID = re.compile(r"\[(R-[0-9]{3,})\] [^/\\]+\.md\Z")


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class RequirementSourceLookupError(RuntimeError):
    """A candidate source could not be read or its identity could not be checked."""

    def __init__(self, path: str, reason: str):
        self.path = path
        super().__init__(f"Requirement source 조회에 실패했습니다: {path} ({reason})")


@dataclass(frozen=True)
class RequirementSourceResolution:
    status: ResolutionStatus
    source: str
    path: str | None = None
    contents: str | None = None
    candidates: tuple[str, ...] = ()


def source_requirement_id(source: str) -> str | None:
    """Read the original ID only from a canonical legacy source filename."""
    match = _SOURCE_ID.fullmatch(source.removeprefix("docs/requirements/"))
    if match is None or _ACTIVE.fullmatch(source) is None:
        return None
    requirement_id = match.group(1)
    return None if int(requirement_id[2:]) == 0 else requirement_id


def _text(path: str, value: str | bytes) -> str:
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RequirementSourceLookupError(path, "UTF-8 문서가 아닙니다") from error


def _identity(path: str, value: str | bytes) -> tuple[str, str, str]:
    text = _text(path, value)
    try:
        requirement_id, _title = requirement_identity(text)
    except (RequirementIdError, RuntimeError) as error:
        raise RequirementSourceLookupError(path, str(error)) from error
    return requirement_id, text, path


def resolve_requirement_source(
    source: str,
    documents: Mapping[str, str | bytes],
    *,
    allow_archive: bool = True,
    allow_id_fallback: bool = True,
) -> RequirementSourceResolution:
    """Resolve an unchanged source string against one observed document set."""
    if _ACTIVE.fullmatch(source) is None:
        raise RequirementSourceLookupError(source, "활성 Requirement source 경로가 아닙니다")

    if source in documents:
        return RequirementSourceResolution(
            ResolutionStatus.RESOLVED, source, source, _text(source, documents[source]),
        )

    archive_path = "docs/requirements/archive/" + source.removeprefix(
        "docs/requirements/"
    )
    if allow_archive and archive_path in documents:
        return RequirementSourceResolution(
            ResolutionStatus.RESOLVED, source, archive_path,
            _text(archive_path, documents[archive_path]),
        )

    if not allow_id_fallback:
        return RequirementSourceResolution(ResolutionStatus.MISSING, source)

    requirement_id = source_requirement_id(source)
    if requirement_id is None:
        return RequirementSourceResolution(ResolutionStatus.MISSING, source)

    candidates = []
    for path, value in sorted(documents.items()):
        if _ACTIVE.fullmatch(path) is None and not (
            allow_archive and _ARCHIVED.fullmatch(path) is not None
        ):
            continue
        candidate_id, text, candidate_path = _identity(path, value)
        if candidate_id == requirement_id:
            candidates.append((candidate_path, text))
    if not candidates:
        return RequirementSourceResolution(ResolutionStatus.MISSING, source)
    if len(candidates) > 1:
        return RequirementSourceResolution(
            ResolutionStatus.AMBIGUOUS, source,
            candidates=tuple(path for path, _text_value in candidates),
        )
    path, text = candidates[0]
    return RequirementSourceResolution(ResolutionStatus.RESOLVED, source, path, text)


def worktree_requirement_documents(root: Path) -> dict[str, bytes]:
    """Read only direct regular Requirement files without following symlinks."""
    documents: dict[str, bytes] = {}
    for relative in ("docs/requirements", "docs/requirements/archive"):
        directory = root
        absent = False
        for part in relative.split("/"):
            directory /= part
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                absent = True
                break
            except OSError as error:
                raise RequirementSourceLookupError(relative, str(error)) from error
            if not stat.S_ISDIR(mode):
                raise RequirementSourceLookupError(relative, "경로가 실제 디렉터리가 아닙니다")
        if absent:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise RequirementSourceLookupError(relative, str(error)) from error
        for path in children:
            if path.suffix != ".md":
                continue
            relative_path = f"{relative}/{path.name}"
            try:
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise RequirementSourceLookupError(
                        relative_path, "일반 파일이 아닙니다",
                    )
                documents[relative_path] = path.read_bytes()
            except RequirementSourceLookupError:
                raise
            except OSError as error:
                raise RequirementSourceLookupError(relative_path, str(error)) from error
    return documents


def reconcile_execplan_sources(path: str, raw: bytes, documents: Mapping[str, str | bytes]) -> bytes:
    """Update only unambiguous active source paths; preserve all other plan bytes."""
    from loop_manager_internal.catalog import execplan_catalog
    plan = execplan_catalog.parse_execplan_text(path, raw.decode('utf-8'), validate_sources=False)
    if plan.get('draft') or '/archive/' in path:
        return raw
    replacements = {}
    for source in plan['sources']:
        if _ACTIVE.fullmatch(source) is None or source in documents:
            continue
        resolved = resolve_requirement_source(source, documents, allow_archive=False)
        if resolved.status is ResolutionStatus.AMBIGUOUS:
            raise RequirementSourceLookupError(source, '같은 R-ID의 source 후보가 여러 개입니다')
        if resolved.status is ResolutionStatus.RESOLVED and resolved.path != source:
            replacements[source.encode('utf-8')] = resolved.path.encode('utf-8')
    if not replacements:
        return raw
    lines = raw.splitlines(keepends=True)
    in_sources = False
    for index, line in enumerate(lines[1:], 1):
        stripped = line.rstrip(b'\r\n')
        if stripped == b'---':
            break
        if stripped == b'sources:':
            in_sources = True
        elif in_sources and stripped.startswith(b'  - '):
            source = stripped[4:]
            if source in replacements:
                lines[index] = b'  - ' + replacements[source] + line[len(stripped):]
        elif stripped and not stripped.startswith(b' '):
            in_sources = False
    result = b''.join(lines)
    updated = execplan_catalog.parse_execplan_text(path, result.decode('utf-8'), validate_sources=False)
    if updated['sources'] != [replacements.get(source.encode(), source.encode()).decode()
                              for source in plan['sources']]:
        raise RequirementSourceLookupError(path, 'sources 선언 형식의 보존을 확인할 수 없습니다')
    return result
