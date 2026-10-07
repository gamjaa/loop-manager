"""Revision-pinned, side-effect-free Requirement archive eligibility."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Callable, Mapping


class ArchiveSnapshotError(ValueError):
    """The supplied catalog, state, or Requirement tree is not unambiguous."""


class ArchiveCollisionError(ArchiveSnapshotError):
    """The archive destination already exists in the pinned tree."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Requirement archive 대상이 이미 존재합니다: {path}")


class ArchiveMoveError(ArchiveSnapshotError):
    """A worktree move is missing or fails byte-preservation checks."""


def stored_requirement_sources(rows) -> tuple[str, ...]:
    """Decode cached source paths only to decide whether archive work exists."""

    sources: set[str] = set()
    for row in rows:
        plan_id = str(row["plan_id"])
        try:
            stored = json.loads(str(row["sources"]))
        except json.JSONDecodeError as error:
            raise ArchiveSnapshotError(
                f"ExecPlan {plan_id}의 SQLite source를 해석할 수 없습니다."
            ) from error
        if not isinstance(stored, list) or any(
            not isinstance(source, str) for source in stored
        ):
            raise ArchiveSnapshotError(
                f"ExecPlan {plan_id}의 SQLite source를 해석할 수 없습니다."
            )
        for source in stored:
            if source in {"docs/PRODUCT.md", "manager:verify-inputs"}:
                continue
            if re.fullmatch(r"docs/requirements/[^/\\]+\.md", source) is None:
                raise ArchiveSnapshotError(
                    f"ExecPlan {plan_id}의 SQLite source가 유효하지 않습니다: {source}"
                )
            sources.add(source)
    return tuple(sorted(sources))


def validated_moves(
    tree: Mapping[bytes, tuple[bytes, bytes]],
    worktree: Path,
    *,
    blob_contents: Callable[[bytes], bytes],
    tracked_contents: Callable[[str], bytes] | None = None,
    ignore_status: Callable[[str], tuple[int, str]] | None = None,
) -> tuple[str, ...]:
    """Validate the actual worktree move set against a pinned base tree."""

    moved = []
    for path, (object_type, object_id) in sorted(tree.items()):
        if re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is None:
            continue
        source = path.decode("utf-8")
        archived = "docs/requirements/archive/" + source.removeprefix(
            "docs/requirements/"
        )
        source_path = worktree / source
        archived_path = worktree / archived
        archived_in_base = archived.encode("utf-8") in tree
        if source_path.is_file():
            if not archived_in_base and archived_path.exists():
                raise ArchiveCollisionError(archived)
            continue
        if archived_in_base:
            raise ArchiveMoveError(
                f"활성 Requirement와 기존 archive의 관계를 확정할 수 없습니다: {source}"
            )
        try:
            archived_mode = archived_path.lstat().st_mode
        except FileNotFoundError as error:
            raise ArchiveMoveError(
                f"Requirement 원본과 archive 대상이 모두 없습니다: {source}"
            ) from error
        if not stat.S_ISREG(archived_mode):
            raise ArchiveMoveError(
                f"Requirement archive 대상이 일반 파일이 아닙니다: {archived}"
            )
        if object_type != b"blob":
            raise ArchiveMoveError(
                f"Requirement 원본 내용을 확인할 수 없습니다: {source}"
            )
        original = blob_contents(object_id)
        if archived_path.read_bytes() != original:
            raise ArchiveMoveError(
                f"Requirement archive 이동 중 내용이 변경됐습니다: {source}"
            )
        if tracked_contents is None:
            if ignore_status is None:
                raise ValueError("archive ignore-status port가 없습니다.")
            ignored_code, ignored_output = ignore_status(archived)
            if ignored_code == 0:
                raise ArchiveMoveError(
                    "Requirement archive 대상이 Git ignore 규칙으로 제외됩니다: "
                    f"{archived}"
                )
            if ignored_code != 1:
                raise ArchiveMoveError(
                    "Requirement archive 대상의 Git 추적 가능 여부를 확인할 수 "
                    f"없습니다: {archived} ({ignored_output[-2000:]})"
                )
        elif tracked_contents(archived) != original:
            raise ArchiveMoveError(
                f"Requirement archive의 게시 내용이 원본과 다릅니다: {source}"
            )
        moved.append(source)
    return tuple(moved)


@dataclass(frozen=True)
class ArchiveSnapshot:
    base_oid: str
    catalog_token: str
    state_token: str
    current_plan_id: str
    candidates: tuple[str, ...]
    related_status: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    move_set: tuple[str, ...] | None = None

    @property
    def token(self) -> str:
        payload = (
            self.base_oid, self.catalog_token, self.state_token,
            self.current_plan_id, self.candidates, self.related_status,
        )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def with_moves(self, moves: tuple[str, ...]) -> "ArchiveSnapshot":
        return replace(self, move_set=tuple(sorted(moves)))


def calculate(
    *,
    base_oid: str,
    catalog: Mapping[str, Mapping[str, object]],
    states: Mapping[str, str],
    active_requirements: tuple[str, ...],
    archived_requirements: frozenset[str],
    current_plan_id: str,
    unverified_historical_ids: frozenset[str] = frozenset(),
    known_statuses: frozenset[str],
    cache_versions: Mapping[str, object] | None = None,
) -> ArchiveSnapshot:
    """Compute candidates from the validated file catalog and one state view.

    Declaration cache fields are deliberately absent from this API. They can
    lag behind a valid file catalog and must never override its source edges.
    """

    if not base_oid or current_plan_id not in states:
        raise ArchiveSnapshotError(f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}")
    for plan_id, status in states.items():
        if status not in known_statuses:
            raise ArchiveSnapshotError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    missing = set(states) - set(catalog)
    unknown = set(catalog) - set(states) - unverified_historical_ids
    if missing or unknown:
        detail = []
        if unknown:
            detail.append("SQLite 상태 없음: " + ", ".join(sorted(unknown)))
        if missing:
            detail.append("Git ExecPlan 없음: " + ", ".join(sorted(missing)))
        raise ArchiveSnapshotError(
            "ExecPlan source 관계 또는 상태를 확정할 수 없습니다 ("
            + "; ".join(detail) + ")."
        )

    catalog_values = []
    for plan_id, plan in sorted(catalog.items()):
        sources = plan.get("sources")
        if not isinstance(sources, (list, tuple)) or any(
            not isinstance(source, str) for source in sources
        ):
            raise ArchiveSnapshotError(f"ExecPlan {plan_id}의 source가 유효하지 않습니다.")
        catalog_values.append((plan_id, tuple(sorted(sources)), plan.get("path"),
                               plan.get("title"), plan.get("depends_on"),
                               plan.get("complexity"), plan.get("risk"),
                               plan.get("priority"), plan.get("content_hash")))
    catalog_token = hashlib.sha256(
        json.dumps(catalog_values, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode()
    ).hexdigest()
    state_values = tuple(sorted(states.items()))
    cache_values = tuple(sorted((cache_versions or {}).items()))
    state_token = hashlib.sha256(
        json.dumps((state_values, cache_values, sorted(unverified_historical_ids)),
                   separators=(",", ":")).encode()
    ).hexdigest()

    candidates = []
    related_status = []
    for source in sorted(active_requirements):
        related = tuple(
            (plan_id, states.get(plan_id, "UNVERIFIED"))
            for plan_id, plan in sorted(catalog.items())
            if source in plan["sources"]
        )
        if not related:
            continue
        related_status.append((source, related))
        if any(
            plan_id in unverified_historical_ids
            or (plan_id != current_plan_id and status != "MERGED")
            for plan_id, status in related
        ):
            continue
        archived = "docs/requirements/archive/" + source.removeprefix(
            "docs/requirements/"
        )
        if archived in archived_requirements:
            raise ArchiveCollisionError(archived)
        candidates.append(source)
    return ArchiveSnapshot(
        base_oid, catalog_token, state_token, current_plan_id,
        tuple(candidates), tuple(related_status),
    )
