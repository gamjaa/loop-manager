"""Identify changed documents that can affect the current Planner result."""
from __future__ import annotations

from pathlib import Path
import re
import stat

from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text
from loop_manager_internal.git.command_adapter import revision_requirement_documents


def _blob(git, repository: Path, revision: str, path: str) -> str | None:
    result = git(repository, "rev-parse", "--verify", f"{revision}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _content_changed(git, repository: Path, old: str, new: str, path: str) -> bool:
    previous = _blob(git, repository, old, path)
    current = _blob(git, repository, new, path)
    if previous == current:
        return False
    if previous and current is None:
        # A completed Requirement/ExecPlan archive move preserves its bytes.
        if path.startswith("docs/requirements/"):
            archived = "docs/requirements/archive/" + path.rsplit("/", 1)[-1]
        elif path.startswith("docs/plans/"):
            archived = "docs/plans/archive/" + path.rsplit("/", 1)[-1]
        else:
            archived = ""
        if archived and _blob(git, repository, new, archived) == previous:
            return False
    return True


_PLAN_PATH = re.compile(r"docs/plans/(?:archive/)?\[(G-\d{3,})\] [^/]+\.md")


def _plan_paths(git, repository: Path, revision: str, plan_ids: set[str]) -> dict[str, str]:
    if not plan_ids:
        return {}
    result = git(repository, "ls-tree", "-r", "-z", "--name-only", revision,
                 "--", "docs/plans")
    paths: dict[str, str] = {}
    for path in result.stdout.split("\0"):
        match = _PLAN_PATH.fullmatch(path)
        if match is None or match.group(1) not in plan_ids:
            continue
        if match.group(1) in paths:
            raise RuntimeError(f"동일 ExecPlan ID의 원격 경로가 경쟁합니다: {match.group(1)}")
        paths[match.group(1)] = path
    return paths


def _declaration(git, repository: Path, revision: str, path: str) -> dict[str, object]:
    result = git(repository, "show", f"{revision}:{path}")
    return parse_execplan_text(path, result.stdout, validate_sources=False)


def relevant_document_changes(
    git, repository: Path, worktree: Path, old: str, new: str,
    edited_paths: list[str], catalog_rows, target_plan_ids=(),
    *, include_active_requirements: bool = False, git_bytes=None,
) -> tuple[str, ...]:
    """Compare remote plan inputs and, when requested, active requirements."""
    changed = set(git(repository, "diff", "--name-only", "-z", old, new, "--",
                      "docs").stdout.split("\0")) - {""}
    if not changed:
        return ()
    rows = {str(row["plan_id"]): row for row in catalog_rows}
    plan_paths = [
        path for path in edited_paths
        if path.startswith("docs/plans/") and path.endswith(".md")
        and path.count("/") == 2
    ]
    explicit_ids = set(target_plan_ids)
    ids = set(explicit_ids)
    for path in plan_paths:
        match = _PLAN_PATH.fullmatch(path)
        if match:
            ids.add(match.group(1))
    old_paths = _plan_paths(git, repository, old, ids)
    new_paths = _plan_paths(git, repository, new, ids)
    relevant = {"docs/PRODUCT.md", "docs/ARCHITECTURE.md",
                "docs/PLANS.md"}
    declarations = []
    dependency_ids: set[str] = set()
    requirement_documents: dict[str, dict[str, bytes]] = {}
    resolved_requirement_paths: dict[str, dict[str, str | None]] = {}

    def requirement_docs(revision: str) -> dict[str, bytes]:
        if revision not in requirement_documents:
            requirement_documents[revision] = (
                revision_requirement_documents(git_bytes, repository, revision)
                if git_bytes is not None else {}
            )
        return requirement_documents[revision]

    def add_sources(revision: str, sources) -> None:
        paths = resolved_requirement_paths.setdefault(revision, {})
        for raw_source in sources:
            source = str(raw_source)
            if source.startswith("manager:"):
                continue
            if not (source.startswith("docs/requirements/")
                    and source.count("/") == 2 and git_bytes is not None):
                relevant.add(source)
                continue
            result = requirement_sources.resolve_requirement_source(
                source, requirement_docs(revision), allow_archive=True,
            )
            if result.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError(
                    f"ExecPlan source가 모호합니다: {source} ({', '.join(result.candidates)})"
                )
            resolved_path = result.path if result.status is requirement_sources.ResolutionStatus.RESOLVED else None
            paths[source] = resolved_path
            relevant.add(resolved_path or source)

    for plan_id in ids:
        previous_path = old_paths.get(plan_id)
        current_path = new_paths.get(plan_id)
        if previous_path is None or current_path is None:
            # A missing target is a validation problem, never an unchanged input.
            relevant.update(path for path in (previous_path, current_path) if path)
            if not (previous_path or current_path):
                if plan_id in explicit_ids:
                    raise RuntimeError(f"평가 대상 ExecPlan이 원격에서 사라졌습니다: {plan_id}")
                # A newly authored local plan has no remote declaration yet.
            continue
        previous_oid = _blob(git, repository, old, previous_path)
        current_oid = _blob(git, repository, new, current_path)
        if previous_oid != current_oid:
            relevant.add(current_path)
        elif previous_path != current_path and (
            previous_path.startswith("docs/plans/archive/")
            != current_path.startswith("docs/plans/archive/")):
            # A verified content-preserving archive move is lifecycle neutral.
            pass
        elif previous_path != current_path:
            relevant.add(current_path)
        for revision, path in ((old, previous_path), (new, current_path)):
            declaration = _declaration(git, repository, revision, path)
            declarations.append((revision, declaration))
            add_sources(revision, declaration["sources"])
            dependency_ids.update(str(value) for value in declaration["depends_on"])
    old_dependencies = _plan_paths(git, repository, old, dependency_ids)
    new_dependencies = _plan_paths(git, repository, new, dependency_ids)
    for revision, declaration in declarations:
        for dependency in declaration["depends_on"]:
            dependency_id = str(dependency)
            path = (old_dependencies if revision == old else new_dependencies).get(dependency_id)
            if path is None:
                raise RuntimeError(f"평가 대상의 dependency 경로가 불명확합니다: {dependency_id}")
            relevant.add(path)
            counterpart = (new_dependencies if revision == old else old_dependencies).get(dependency_id)
            if counterpart:
                relevant.add(counterpart)
    local_requirement_documents = (
        requirement_sources.worktree_requirement_documents(worktree)
        if plan_paths else {}
    )
    for path in plan_paths:
        file = worktree / path
        try:
            if not stat.S_ISREG(file.lstat().st_mode):
                continue
            parsed = parse_execplan_text(
                path, file.read_text(encoding="utf-8"),
                validate_sources=False,
            )
        except (OSError, UnicodeError, RuntimeError):
            continue  # The ordinary Planner validation reports incomplete files.
        for source in parsed["sources"]:
            source = str(source)
            if source.startswith("manager:"):
                continue
            if source.startswith("docs/requirements/") and source.count("/") == 2:
                result = requirement_sources.resolve_requirement_source(
                    source, local_requirement_documents, allow_archive=True,
                )
                if result.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                    raise RuntimeError(
                        f"ExecPlan source가 모호합니다: {source} ({', '.join(result.candidates)})"
                    )
                relevant.add(result.path or source)
            else:
                relevant.add(source)
        for dependency in parsed["depends_on"]:
            row = rows.get(str(dependency))
            if row is not None:
                relevant.add(str(row["path"]))
    if include_active_requirements:
        # General planning assesses active requirements beyond the sources of
        # existing plans, including when there is no plan declaration yet.
        relevant.update(path for path in changed if path == "docs/PRODUCT.md"
                        or (path.startswith("docs/requirements/")
                            and path.count("/") == 2 and path.endswith(".md")))
    # A renamed target is reported by its new path even though the old path
    # alone appears in a deletion-only diff.
    changed_inputs = {
        path for path in changed & relevant
        if _content_changed(git, repository, old, new, path)
    }
    if git_bytes is not None:
        by_source: dict[str, dict[str, str | None]] = {}
        for revision, paths in resolved_requirement_paths.items():
            for source, path in paths.items():
                by_source.setdefault(source, {})[revision] = path
        for paths in by_source.values():
            previous_path, current_path = paths.get(old), paths.get(new)
            if previous_path and current_path and previous_path != current_path:
                changed_inputs.discard(previous_path)
                changed_inputs.discard(current_path)
                previous = requirement_docs(old).get(previous_path)
                current = requirement_docs(new).get(current_path)
                if previous != current:
                    changed_inputs.add(current_path)
    for plan_id in ids:
        before, after = old_paths.get(plan_id), new_paths.get(plan_id)
        if before and after and before != after:
            changed_inputs.discard(before)
            # A READY target becoming archived cannot be an innocuous move.
            if (plan_id in explicit_ids or
                _blob(git, repository, old, before) != _blob(git, repository, new, after) or
                before.startswith("docs/plans/archive/") == after.startswith("docs/plans/archive/")):
                changed_inputs.add(after)
    for dependency_id in dependency_ids:
        before, after = old_dependencies.get(dependency_id), new_dependencies.get(dependency_id)
        if before and after and before != after:
            changed_inputs.discard(before)
            changed_inputs.discard(after)
            if _blob(git, repository, old, before) != _blob(git, repository, new, after) or (
                before.startswith("docs/plans/archive/") == after.startswith("docs/plans/archive/")
            ):
                changed_inputs.add(after)
    return tuple(sorted(changed_inputs))
