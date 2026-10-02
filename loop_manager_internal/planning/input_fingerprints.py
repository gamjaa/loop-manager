"""planning input fingerprints boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import execplan_catalog
from pathlib import Path
import hashlib
import sqlite3


def _planning_input_digest(entries: list[tuple[bytes, bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-planning-input-v2\0")
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _specification_input_digest(
    entries: list[tuple[bytes, bytes, bytes]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-specification-input-v1\0")
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


def _planning_input_fingerprint(effects: InternalPlanningInputFingerprintEffects, repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in effects._git_tree_entries(repo, revision).items():
        if effects._is_planning_input(path):
            if object_type == b"blob":
                contents = effects.git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            else:
                contents = object_id
            entries.append((path, object_type, contents))
    return effects._planning_input_digest(entries)


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


def _specification_input_fingerprint(effects: InternalSpecificationInputFingerprintEffects, repo: Path, revision: str) -> str:
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
    return effects._specification_input_digest(entries)


@dataclass(frozen=True)
class SpecificationInputFingerprintEffects:
    """Named effects for specification_input_fingerprint; no entry-point/module access."""
    _specification_input_fingerprint: Callable[..., object]


def specification_input_fingerprint(effects: SpecificationInputFingerprintEffects, repo: Path, base: str) -> str:
    return effects._specification_input_fingerprint(repo, f"origin/{base}")


@dataclass(frozen=True)
class IsRequirementArchiveOnlyChangeEffects:
    """Named effects for _is_requirement_archive_only_change; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    _is_planning_input: Callable[..., object]


def _is_requirement_archive_only_change(
    effects: IsRequirementArchiveOnlyChangeEffects, repo: Path,
    previous_revision: str,
    current_revision: str,
) -> bool:
    previous_tree = effects._git_tree_entries(repo, previous_revision)
    current_tree = effects._git_tree_entries(repo, current_revision)
    archive_prefix = b"docs/requirements/archive/"

    def relevant(path: bytes) -> bool:
        return effects._is_planning_input(path) or path.startswith(archive_prefix)

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
    return execplan_catalog.classify_archive_only_tree_change(
        previous, current
    ).requirement_only


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def requirement_archive_path(source: str) -> str:
    return execplan_catalog.requirement_archive_path(source)


def parse_frontmatter(text: str) -> dict[str, object]:
    return execplan_catalog.parse_frontmatter(text)


@dataclass(frozen=True)
class GeneralPlanningRequiredEffects:
    """Named effects for _general_planning_required; no entry-point/module access."""
    _is_requirement_archive_only_change: Callable[..., object]
    _planning_input_fingerprint: Callable[..., object]


def _general_planning_required(
    effects: GeneralPlanningRequiredEffects, row: sqlite3.Row,
    repo_path: Path,
    base_sha: str,
    current_fingerprint: str,
) -> bool:
    if row["planning_fingerprint"] == current_fingerprint:
        return False
    if row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = effects._planning_input_fingerprint(
            repo_path, str(row["processed_sha"])
        )
        if processed_fingerprint == current_fingerprint:
            return False
        if effects._is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha
        ):
            return False
    return True
