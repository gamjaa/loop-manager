"""Plan deterministic Requirement identity edits from one observed snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
from typing import Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.catalog.frontmatter import (
    requirement_identity,
)


_ACTIVE = re.compile(r"docs/requirements/[^/\\]+\.md\Z")
_ARCHIVED = re.compile(r"docs/requirements/archive/[^/\\]+\.md\Z")
_HEADING = re.compile(r"# \[(R-[0-9]{3,})\] (.+)\Z")
_INLINE_LINK = re.compile(
    r"\]\((<[^>\r\n]*>|(?:\\.|[^()\s])+)([ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^()\r\n]*\))[ \t]*)?\)"
)
_LINK_DEFINITION = re.compile(
    r"^([ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*)(<[^>\r\n]*>|(?:\\.|[^\s])+)(.*)$"
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDENTED_CODE = re.compile(r"^(?: {4}| {0,3}\t)")
_BLOCKQUOTE = re.compile(r"^ {0,3}>[ \t]?")
_LIST_MARKER = re.compile(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?P<padding> +)")
_USER_DECISION = re.compile(r"^(#{1,6})[ \t]+User Decision(?:[ \t]+#+)?[ \t]*$")
_ANY_HEADING = re.compile(r"^(#{1,6})(?:[ \t]+|$)")
_IGNORED_SOURCES = frozenset({"docs/PRODUCT.md", "manager:verify-inputs"})


@dataclass(frozen=True)
class RequirementCorrection:
    source_path: str
    target_path: str
    old_id: str
    new_id: str
    title: str
    before: bytes
    after: bytes


@dataclass(frozen=True)
class DeferredCorrection:
    path: str
    reason: str


@dataclass(frozen=True)
class RequirementCorrectionPlan:
    corrections: tuple[RequirementCorrection, ...]
    deferred: tuple[DeferredCorrection, ...]
    updated_documents: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class _Requirement:
    path: str
    raw: bytes
    text: str | None
    requirement_id: str | None
    title: str | None
    archived: bool
    frontmatter: bool
    error: str | None = None


def _bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _requirement(path: str, value: str | bytes) -> _Requirement:
    if _ACTIVE.fullmatch(path) is None and _ARCHIVED.fullmatch(path) is None:
        raise ValueError(f"Requirement 경로가 유효하지 않습니다: {path}")
    raw = _bytes(value)
    try:
        text = raw.decode("utf-8")
        requirement_id, title = requirement_identity(text)
        frontmatter = text.removeprefix("\ufeff").splitlines()[:1] == ["---"]
    except (UnicodeDecodeError, ValueError, RuntimeError) as error:
        return _Requirement(
            path, raw, None, None, None, _ARCHIVED.fullmatch(path) is not None,
            False, str(error),
        )
    return _Requirement(
        path, raw, text, requirement_id, title,
        _ARCHIVED.fullmatch(path) is not None, frontmatter,
    )


def _filename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _text_order(path: str) -> tuple[str, str]:
    # Python's string order is Unicode code point order and does not depend on locale.
    return _filename(path), path


def _safe_title(title: str) -> bool:
    return bool(title.strip()) and not any(
        ord(char) < 32 or char in '<>:"/\\|?*' for char in title
    )


def _safe_filename(requirement_id: str, title: str) -> bool:
    if not _safe_title(title):
        return False
    try:
        return len(_filename(_canonical_path(requirement_id, title)).encode("utf-8")) <= 255
    except UnicodeEncodeError:
        return False


def _heading(text: str) -> tuple[int, str, str] | None:
    lines = text.removeprefix("\ufeff").splitlines()
    start = 0
    if lines and lines[0] == "---":
        try:
            start = next(index for index in range(1, len(lines)) if lines[index] == "---") + 1
        except StopIteration:
            return None
    for index, line in enumerate(lines[start:], start):
        if not line.startswith("# "):
            continue
        match = _HEADING.fullmatch(line)
        if match is None or not match.group(2).strip():
            return None
        return index, match.group(1), match.group(2).strip()
    return None


def _edit_identity(text: str, requirement_id: str, title: str) -> str:
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text.removeprefix(bom)
    lines = body.splitlines(keepends=True)
    start = 0
    if lines and lines[0].rstrip("\r\n") == "---":
        try:
            end = next(
                index for index in range(1, len(lines))
                if lines[index].rstrip("\r\n") == "---"
            )
        except StopIteration as error:
            raise ValueError("frontmatter 종료 구분자가 없습니다") from error
        frontmatter = _replace_frontmatter_field(
            "".join(lines[:end + 1]), "id", requirement_id,
        )
        frontmatter = _replace_frontmatter_field(frontmatter, "title", title)
        lines[:end + 1] = [frontmatter]
        start = 1

    body = "".join(lines)
    body_lines = body.splitlines(keepends=True)
    heading_index = next(
        (index for index, line in enumerate(body_lines[start:], start)
         if line.rstrip("\r\n").startswith("# ")),
        None,
    )
    if heading_index is None:
        raise ValueError("Requirement 첫 제목이 없습니다")
    line = body_lines[heading_index]
    line_content = line.rstrip("\r\n")
    if _HEADING.fullmatch(line_content) is None:
        raise ValueError("Requirement 첫 제목 형식이 유효하지 않습니다")
    newline = line[len(line_content):]
    trailing = line_content[len(line_content.rstrip(" \t")):]
    body_lines[heading_index] = f"# [{requirement_id}] {title}{trailing}{newline}"
    return bom + "".join(body_lines)


def _replace_frontmatter_field(frontmatter: str, key: str, value: str) -> str:
    lines = frontmatter.splitlines(keepends=True)
    pattern = re.compile(rf"^([ \t]*{re.escape(key)}[ \t]*:[ \t]*)(.*?)([ \t]*)$")
    matches = 0
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        match = pattern.fullmatch(content)
        if match is None:
            continue
        newline = line[len(content):]
        lines[index] = f"{match.group(1)}{value}{match.group(3)}{newline}"
        matches += 1
    if matches != 1:
        raise ValueError(f"frontmatter {key}를 보존해 갱신할 수 없습니다")
    return "".join(lines)


def _resolve_link_path(destination: str, document_path: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(destination)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    target = posixpath.normpath(
        posixpath.join(posixpath.dirname(document_path), unquote(parsed.path))
    )
    if target == ".." or target.startswith("../"):
        return None
    return target, parsed.query, parsed.fragment


def _rewrite_destination(
    destination: str,
    document_path: str,
    renames: Mapping[str, str],
) -> str:
    resolved = _resolve_link_path(destination, document_path)
    if resolved is None:
        return destination
    target, query, fragment = resolved
    renamed = renames.get(target)
    if renamed is None:
        return destination
    relative = posixpath.relpath(renamed, posixpath.dirname(document_path) or ".")
    return urlunsplit(("", "", quote(relative, safe="/-._~"), query, fragment))


def _inline_code_spans(
    content: str, active_delimiter: int | None = None,
) -> tuple[tuple[tuple[int, int], ...], int | None]:
    runs = tuple(re.finditer(r"`+", content))
    spans: list[tuple[int, int]] = []
    cursor = 0
    if active_delimiter is not None:
        closing = next(
            (run for run in runs if len(run.group()) == active_delimiter), None,
        )
        if closing is None:
            return ((0, len(content)),), active_delimiter
        spans.append((0, closing.end()))
        cursor = closing.end()

    while True:
        opening = next((run for run in runs if run.start() >= cursor), None)
        if opening is None:
            return tuple(spans), None
        delimiter = len(opening.group())
        closing = next(
            (
                run for run in runs
                if run.start() >= opening.end() and len(run.group()) == delimiter
            ),
            None,
        )
        if closing is None:
            spans.append((opening.start(), len(content)))
            return tuple(spans), delimiter
        spans.append((opening.start(), closing.end()))
        cursor = closing.end()


def _rewrite_markdown_links(
    text: str, document_path: str, renames: Mapping[str, str],
) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    output: list[str] = []
    in_frontmatter = lines[0].removeprefix("\ufeff").rstrip("\r\n") == "---"
    frontmatter_open = in_frontmatter
    fence: tuple[str, int, int] | None = None
    # Zero denotes a quote; list items store their content indentation.
    containers: list[int] = []
    inline_code_delimiter: int | None = None
    decision_level: int | None = None

    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if frontmatter_open:
            output.append(line)
            if index and content == "---":
                frontmatter_open = False
            continue
        # Normalize only the inspection copy, preserving all original bytes.
        block_content = content
        retained = 0
        for indent in containers:
            if indent == 0:
                quote_match = _BLOCKQUOTE.match(block_content)
                if quote_match is None:
                    break
                block_content = block_content[quote_match.end():]
            elif not block_content.strip():
                block_content = ""
            elif block_content.expandtabs(4).startswith(" " * indent):
                block_content = block_content.expandtabs(4)[indent:]
            else:
                break
            retained += 1
        del containers[retained:]
        if fence is not None:
            marker, size, fence_depth = fence
            if retained < fence_depth:
                fence = None
            else:
                if re.fullmatch(
                    rf" {{0,3}}{re.escape(marker)}{{{size},}}[ \t]*", block_content,
                ):
                    fence = None
                output.append(line)
                continue

        if inline_code_delimiter is not None:
            code_spans, inline_code_delimiter = _inline_code_spans(
                content, inline_code_delimiter,
            )
        else:
            # Discover new containers only outside fenced/inline code. A list
            # continuation uses the indentation recorded on its opening line.
            while True:
                quote_match = _BLOCKQUOTE.match(block_content)
                if quote_match is not None:
                    containers.append(0)
                    block_content = block_content[quote_match.end():]
                    continue
                list_content = block_content.expandtabs(4)
                list_match = _LIST_MARKER.match(list_content)
                if list_match is None:
                    break
                padding = len(list_match.group("padding"))
                indent = list_match.start("padding") + (padding if padding <= 4 else 1)
                containers.append(indent)
                block_content = list_content[indent:]
            fence_match = _FENCE.match(block_content)
            if fence_match is not None:
                marker = fence_match.group(1)
                fence = (marker[0], len(marker), len(containers))
                output.append(line)
                continue

            heading_match = _ANY_HEADING.match(content)
            if decision_level is not None and heading_match is not None:
                if len(heading_match.group(1)) <= decision_level:
                    decision_level = None
            decision_match = _USER_DECISION.fullmatch(content)
            if decision_match is not None:
                decision_level = len(decision_match.group(1))
                output.append(line)
                continue
            if decision_level is not None:
                output.append(line)
                continue
            if _INDENTED_CODE.match(block_content) is not None:
                output.append(line)
                continue

            code_spans, inline_code_delimiter = _inline_code_spans(content)

        definition = _LINK_DEFINITION.fullmatch(content)
        if definition is not None and not any(
            definition.span(2)[0] < end and start < definition.span(2)[1]
            for start, end in code_spans
        ):
            raw = definition.group(2)
            angle = raw.startswith("<") and raw.endswith(">")
            destination = raw[1:-1] if angle else raw
            rewritten = _rewrite_destination(destination, document_path, renames)
            if rewritten != destination:
                wrapped = f"<{rewritten}>" if angle else rewritten
                content = (
                    content[:definition.start(2)] + wrapped
                    + content[definition.end(2):]
                )
        else:
            replacements = []
            for match in _INLINE_LINK.finditer(content):
                start, end = match.span(1)
                if any(start < code_end and code_start < end
                       for code_start, code_end in code_spans):
                    continue
                raw = match.group(1)
                angle = raw.startswith("<") and raw.endswith(">")
                destination = raw[1:-1] if angle else raw
                rewritten = _rewrite_destination(destination, document_path, renames)
                if rewritten != destination:
                    replacements.append((start, end, f"<{rewritten}>" if angle else rewritten))
            for start, end, replacement in reversed(replacements):
                content = content[:start] + replacement + content[end:]
        output.append(content + line[len(line.rstrip("\r\n")):])
    return "".join(output)


def _canonical_path(requirement_id: str, title: str) -> str:
    return f"docs/requirements/[{requirement_id}] {title}.md"


def _history_keeper(
    paths: Iterable[str], first_seen_order: Mapping[str, int | None],
) -> str:
    ordered = tuple(paths)
    ranks = [first_seen_order.get(path) for path in ordered]
    if any(
        not isinstance(rank, int) or isinstance(rank, bool) or rank < 0
        for rank in ranks
    ):
        return min(ordered, key=_text_order)
    earliest = min(rank for rank in ranks if rank is not None)
    return min(
        (path for path in ordered if first_seen_order[path] == earliest),
        key=_text_order,
    )


def plan_requirement_corrections(
    requirement_documents: Mapping[str, str | bytes],
    *,
    referenced_sources: Iterable[str] = (),
    references_complete: bool = False,
    first_seen_order: Mapping[str, int | None] | None = None,
    editable_documents: Mapping[str, str | bytes] | None = None,
) -> RequirementCorrectionPlan:
    """Select and minimally edit active Requirement identities without side effects.

    ``referenced_sources`` must combine published plans, drafts, and persisted
    work relationships. ``first_seen_order`` is observed from the pinned base
    branch history; equal or unavailable order falls back to filename text order.
    """
    parsed = {
        path: _requirement(path, value)
        for path, value in requirement_documents.items()
    }
    active = {path: item for path, item in parsed.items() if not item.archived}
    valid = {path: item for path, item in parsed.items() if item.requirement_id is not None}
    identity_complete = len(valid) == len(parsed)
    order = first_seen_order or {}

    by_id: dict[str, list[_Requirement]] = {}
    for item in valid.values():
        assert item.requirement_id is not None
        by_id.setdefault(item.requirement_id, []).append(item)

    referenced_paths: set[str] = set()
    unknown_paths: set[str] = set()
    source_observations = []
    unresolved_sources: dict[str, str] = {}
    global_unknown = not references_complete or not identity_complete
    source_values = []
    for source in referenced_sources:
        if not isinstance(source, str):
            global_unknown = True
            continue
        source_values.append(source)
    source_values = tuple(sorted(set(source_values)))
    for source in source_values:
        if source in _IGNORED_SOURCES:
            continue
        try:
            source_id = requirement_sources.source_requirement_id(source)
        except ValueError:
            source_id = None
            global_unknown = True
        try:
            resolution = requirement_sources.resolve_requirement_source(
                source, requirement_documents, allow_archive=True,
            )
        except (requirement_sources.RequirementSourceLookupError, UnicodeError, ValueError) as error:
            source_observations.append((source, None, str(error)))
            unresolved_sources[source] = f"기존 source를 확인할 수 없어 미확인으로 남겼습니다: {error}"
            if source_id is None:
                global_unknown = True
            else:
                unknown_paths.update(
                    item.path for item in by_id.get(source_id, ()) if not item.archived
                )
            continue
        source_observations.append((source, resolution, None))
        if resolution.status is requirement_sources.ResolutionStatus.RESOLVED:
            assert resolution.path is not None
            referenced_paths.add(resolution.path)
        elif resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
            unknown_paths.update(resolution.candidates)
            unresolved_sources[source] = "기존 source 후보가 여러 개라 미확인으로 남겼습니다"
        else:
            unresolved_sources[source] = "기존 source 문서를 찾을 수 없어 미확인으로 남겼습니다"
            if source_id is None:
                # An unidentifiable source cannot be assigned to a current document.
                global_unknown = True
            else:
                unknown_paths.update(
                    item.path for item in by_id.get(source_id, ()) if not item.archived
                )
    if global_unknown:
        unknown_paths.update(active)

    groups = {
        requirement_id: items
        for requirement_id, items in by_id.items()
        if len(items) > 1
    }
    candidates: set[str] = set()
    deferred_reasons: dict[str, str] = {
        path: f"Requirement 식별 정보를 확인할 수 없습니다: {item.error}"
        for path, item in parsed.items() if item.error is not None
    }
    deferred_reasons.update(unresolved_sources)
    for requirement_id, items in groups.items():
        active_items = [item for item in items if not item.archived]
        if not active_items:
            continue
        archived_exists = any(item.archived for item in items)
        protected = {
            item.path for item in active_items
            if item.path in referenced_paths or item.path in unknown_paths
        }
        if protected:
            selected = {item.path for item in active_items} - protected
            if not selected:
                for path in protected:
                    deferred_reasons.setdefault(
                        path, "중복 ID의 기존 source 관계를 보존하거나 참조를 확인할 수 없어 보류했습니다",
                    )
        elif archived_exists:
            selected = {item.path for item in active_items}
        else:
            keeper = _history_keeper(
                (item.path for item in active_items), order,
            )
            selected = {item.path for item in active_items if item.path != keeper}
        candidates.update(selected)

    maximum = max(
        (int(item.requirement_id[2:]) for item in valid.values()
         if item.requirement_id is not None),
        default=0,
    )
    assignments: dict[str, str] = {}
    occupied = {path.casefold() for path in requirement_documents}
    reserved: set[str] = set()
    next_number = maximum + 1
    for path in sorted(candidates, key=_text_order):
        item = active[path]
        assert item.requirement_id is not None and item.title is not None
        if item.error or item.text is None or not _safe_filename(
            f"R-{next_number:03d}", item.title,
        ):
            deferred_reasons[path] = "제목을 파일명으로 사용할 수 없어 ID 교정을 보류했습니다"
            continue
        if not identity_complete:
            deferred_reasons[path] = "모든 활성·archive 원본 ID를 확인할 수 없어 다음 번호를 배정하지 않았습니다"
            continue
        new_id = f"R-{next_number:03d}"
        try:
            _edit_identity(item.text, new_id, item.title)
        except ValueError as error:
            deferred_reasons[path] = f"Requirement 식별 표현을 함께 갱신할 수 없어 보류했습니다: {error}"
            continue
        destination = _canonical_path(new_id, item.title)
        if destination.casefold() in occupied or destination.casefold() in reserved:
            deferred_reasons[path] = f"교정 목적지가 이미 있어 보류했습니다: {destination}"
            continue
        assignments[path] = new_id
        reserved.add(destination.casefold())
        next_number += 1

    proposed: dict[str, RequirementCorrection] = {}
    for path, item in active.items():
        if item.requirement_id is None or item.title is None or item.text is None:
            continue
        new_id = assignments.get(path, item.requirement_id)
        destination = _canonical_path(new_id, item.title)
        heading = _heading(item.text.removeprefix("\ufeff"))
        heading_mismatch = item.frontmatter and (
            heading is None or heading[1:] != (item.requirement_id, item.title)
        )
        needs_change = (
            new_id != item.requirement_id
            or destination != path
            or heading_mismatch
        )
        if not needs_change:
            continue
        if path in deferred_reasons:
            continue
        if destination != path and path in unknown_paths:
            deferred_reasons[path] = "참조 관계가 미확인이라 source identity가 바뀔 수 있는 rename을 보류했습니다"
            continue
        if not _safe_filename(new_id, item.title):
            deferred_reasons[path] = "제목을 파일명으로 사용할 수 없어 교정을 보류했습니다"
            continue
        destination_occupied = any(
            existing != path and existing.casefold() == destination.casefold()
            for existing in requirement_documents
        )
        if destination != path and destination_occupied:
            deferred_reasons[path] = f"교정 목적지가 이미 있어 보류했습니다: {destination}"
            continue
        try:
            after = _edit_identity(item.text, new_id, item.title).encode("utf-8")
        except ValueError as error:
            deferred_reasons[path] = f"Requirement 식별 표현을 함께 갱신할 수 없어 보류했습니다: {error}"
            continue
        if after != item.raw or destination != path:
            proposed[path] = RequirementCorrection(
                path, destination, item.requirement_id, new_id, item.title,
                item.raw, after,
            )

    destinations: dict[str, list[str]] = {}
    for path, correction in proposed.items():
        destinations.setdefault(correction.target_path.casefold(), []).append(path)
    for paths in destinations.values():
        if len(paths) < 2:
            continue
        for path in paths:
            proposed.pop(path)
            deferred_reasons[path] = "여러 Requirement 교정의 목적지가 같아 모두 보류했습니다"

    # Rebuild after every veto: removing one edit can make another source ambiguous.
    while True:
        final_requirements = dict(requirement_documents)
        rename_map = {
            correction.source_path: correction.target_path
            for correction in proposed.values()
            if correction.source_path != correction.target_path
        }
        for correction in proposed.values():
            if correction.source_path != correction.target_path:
                final_requirements.pop(correction.source_path, None)
            final_requirements[correction.target_path] = correction.after

        unsafe_renames: set[str] = set()
        for source, before, error in source_observations:
            if before is None or error is not None:
                continue
            try:
                source_id = requirement_sources.source_requirement_id(source)
            except ValueError:
                source_id = None
            if before.status is requirement_sources.ResolutionStatus.MISSING:
                try:
                    after = requirement_sources.resolve_requirement_source(
                        source, final_requirements, allow_archive=True,
                    )
                except (requirement_sources.RequirementSourceLookupError, UnicodeError, ValueError):
                    continue
                if after.status is not requirement_sources.ResolutionStatus.MISSING:
                    unsafe_renames.update(
                        path for path, correction in proposed.items()
                        if correction.new_id == source_id
                    )
                continue
            if before.status is not requirement_sources.ResolutionStatus.RESOLVED:
                continue
            assert before.path is not None
            expected = rename_map.get(before.path, before.path)
            try:
                after = requirement_sources.resolve_requirement_source(
                    source, final_requirements, allow_archive=True,
                )
            except (requirement_sources.RequirementSourceLookupError, UnicodeError):
                unsafe_renames.add(before.path)
                continue
            if after.status is not requirement_sources.ResolutionStatus.RESOLVED or after.path != expected:
                unsafe_renames.add(before.path)

        vetoes = unsafe_renames.intersection(rename_map)
        if not vetoes:
            break
        for path in vetoes:
            proposed.pop(path)
            deferred_reasons[path] = "기존 source를 rename 뒤 같은 문서로 유일하게 확인할 수 없어 보류했습니다"

    # Existing source targets can veto a path change after the complete preview.
    return _finish_plan(active, proposed, deferred_reasons, editable_documents)


def _finish_plan(
    active: Mapping[str, _Requirement],
    corrections: Mapping[str, RequirementCorrection],
    deferred_reasons: Mapping[str, str],
    editable_documents: Mapping[str, str | bytes] | None,
) -> RequirementCorrectionPlan:
    renames = {
        correction.source_path: correction.target_path
        for correction in corrections.values()
        if correction.source_path != correction.target_path
    }
    final_active: dict[str, bytes] = {}
    for path, item in active.items():
        correction = corrections.get(path)
        if correction is None:
            final_active[path] = item.raw
        else:
            final_active[correction.target_path] = correction.after

    editable = dict(editable_documents or {})
    for path, item in active.items():
        editable.setdefault(path, item.raw)
    updated: dict[str, bytes] = {}
    for path, value in editable.items():
        if "/archive/" in f"/{path}/" or path.startswith("archive/"):
            continue
        correction = corrections.get(path)
        target_path = correction.target_path if correction is not None else path
        raw = final_active.get(target_path, _bytes(value))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        rewritten = _rewrite_markdown_links(text, target_path, renames).encode("utf-8")
        original = _bytes(value)
        if correction is not None or rewritten != original:
            updated[target_path] = rewritten

    finalized = []
    for path, correction in corrections.items():
        after = updated.get(correction.target_path, correction.after)
        finalized.append(RequirementCorrection(
            correction.source_path, correction.target_path,
            correction.old_id, correction.new_id, correction.title,
            correction.before, after,
        ))
    deferred = tuple(
        DeferredCorrection(path, reason)
        for path, reason in sorted(deferred_reasons.items(), key=lambda item: _text_order(item[0]))
    )
    return RequirementCorrectionPlan(
        tuple(sorted(finalized, key=lambda item: _text_order(item.source_path))),
        deferred,
        tuple(sorted(updated.items())),
    )
