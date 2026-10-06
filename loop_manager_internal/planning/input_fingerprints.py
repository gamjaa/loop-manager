"""planning input fingerprints boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import execplan_catalog
from pathlib import Path
import hashlib
from loop_manager_internal.agents import common_guidance


def _input_digest(entries: list[tuple[bytes, bytes, bytes]], version: bytes,
                  guidance_hash: str | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(version)
    digest.update((guidance_hash or common_guidance.runtime_content_hash()).encode("ascii"))
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        for part in (path, object_type, contents):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def _planning_input_digest(entries: list[tuple[bytes, bytes, bytes]], *, guidance_hash: str | None = None) -> str:
    return _input_digest(entries, b"loop-manager-planning-input-v3\0", guidance_hash)


def _nonplan_input_digest(entries: list[tuple[bytes, bytes, bytes]], *, guidance_hash: str | None = None) -> str:
    return _input_digest(entries, b"loop-manager-planning-input-v2\0", guidance_hash)


def _specification_input_digest(
    entries: list[tuple[bytes, bytes, bytes]],
    *, guidance_hash: str | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-specification-input-v1\0")
    digest.update((guidance_hash or common_guidance.runtime_content_hash()).encode("ascii"))
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


@dataclass(frozen=True)
class InternalPlanningInputFingerprintEffects:
    """Named effects for _planning_input_fingerprint; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    _is_planning_input: Callable[..., object]
    _planning_input_digest: Callable[..., object]
    git_bytes: Callable[..., object]


def _planning_input_fingerprint(effects: InternalPlanningInputFingerprintEffects, repo: Path, revision: str,
                                *, guidance_hash: str | None = None) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in effects._git_tree_entries(repo, revision).items():
        if effects._is_planning_input(path):
            if object_type == b"blob":
                contents = effects.git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            else:
                contents = object_id
            entries.append((path, object_type, contents))
    return effects._planning_input_digest(entries, **({'guidance_hash': guidance_hash} if guidance_hash else {}))


def _nonplan_input_fingerprint(
    effects: InternalPlanningInputFingerprintEffects, repo: Path, revision: str,
    *, guidance_hash: str | None = None,
) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in effects._git_tree_entries(repo, revision).items():
        if not effects._is_planning_input(path) or execplan_catalog.is_active_execplan_path(path):
            continue
        contents = (
            effects.git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            if object_type == b"blob" else object_id
        )
        entries.append((path, object_type, contents))
    return _nonplan_input_digest(entries, guidance_hash=guidance_hash)


@dataclass(frozen=True)
class PlanningInputFingerprintEffects:
    """Named effects for planning_input_fingerprint; no entry-point/module access."""
    _planning_input_fingerprint: Callable[..., object]


def planning_input_fingerprint(effects: PlanningInputFingerprintEffects, repo: Path, base: str) -> str:
    return effects._planning_input_fingerprint(repo, f"origin/{base}")


@dataclass(frozen=True)
class InternalSpecificationInputFingerprintEffects:
    """Named effects for _specification_input_fingerprint; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    _is_specification_input: Callable[..., object]
    _specification_input_digest: Callable[..., object]
    git_bytes: Callable[..., object]


def _specification_input_fingerprint(effects: InternalSpecificationInputFingerprintEffects, repo: Path, revision: str,
                                     *, guidance_hash: str | None = None) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in effects._git_tree_entries(repo, revision).items():
        if not effects._is_specification_input(path):
            continue
        contents = (
            effects.git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            if object_type == b"blob"
            else object_id
        )
        entries.append((path, object_type, contents))
    return effects._specification_input_digest(entries, **({'guidance_hash': guidance_hash} if guidance_hash else {}))


@dataclass(frozen=True)
class SpecificationInputFingerprintEffects:
    """Named effects for specification_input_fingerprint; no entry-point/module access."""
    _specification_input_fingerprint: Callable[..., object]


def specification_input_fingerprint(effects: SpecificationInputFingerprintEffects, repo: Path, base: str) -> str:
    return effects._specification_input_fingerprint(repo, f"origin/{base}")


@dataclass(frozen=True)
class PlanDeclarationsEffects:
    _git_tree_entries: Callable[..., object]
    git_bytes: Callable[..., object]


def plan_declarations(
    effects: PlanDeclarationsEffects, repo: Path, revision: str,
) -> dict[str, tuple[str, str]]:
    """Read active plans from one immutable Git tree, including path identity."""
    entries: dict[str, tuple[str, str]] = {}
    for path, (object_type, object_id) in effects._git_tree_entries(repo, revision).items():
        if not execplan_catalog.is_active_execplan_path(path):
            continue
        identity = (
            hashlib.sha256(effects.git_bytes(
                repo, "cat-file", "blob", object_id.decode("ascii")
            )).hexdigest()
            if object_type == b"blob" else object_id.decode("ascii")
        )
        entries[path.decode("utf-8", "surrogateescape")] = (
            object_type.decode("ascii"), identity,
        )
    return entries


@dataclass(frozen=True)
class IsRequirementArchiveOnlyChangeEffects:
    """Named effects for _is_requirement_archive_only_change; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    _is_planning_input: Callable[..., object]


def _is_requirement_archive_only_change(
    effects: IsRequirementArchiveOnlyChangeEffects, repo: Path,
    previous_revision: str,
    current_revision: str,
    *, verified_plan_declarations: bool = False,
    allow_execplan_only: bool = False,
) -> bool:
    previous_tree = effects._git_tree_entries(repo, previous_revision)
    current_tree = effects._git_tree_entries(repo, current_revision)
    archive_prefix = b"docs/requirements/archive/"
    # The caller must match the durable verified declaration baseline first.
    # Recovery of an unfinished Planner result uses the strict default.
    # Final receipt reuse may additionally accept a verified ExecPlan-only move.

    def relevant(path: bytes) -> bool:
        return (effects._is_planning_input(path) or path.startswith(archive_prefix)
                or (verified_plan_declarations and path.startswith(b"docs/plans/archive/")))

    previous = {
        path.decode("utf-8"): value
        for path, value in previous_tree.items()
        if relevant(path)
    }
    current = {
        path.decode("utf-8"): value
        for path, value in current_tree.items()
        if relevant(path)
    }
    change = execplan_catalog.classify_archive_only_tree_change(previous, current)
    return change.requirement_only or (
        verified_plan_declarations and change.archive_only
        and (allow_execplan_only
             or any(move.kind is execplan_catalog.ArchiveDocumentKind.REQUIREMENT
                    for move in change.moves))
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def requirement_archive_path(source: str) -> str:
    return execplan_catalog.requirement_archive_path(source)


def parse_frontmatter(text: str) -> dict[str, object]:
    return execplan_catalog.parse_frontmatter(text)
