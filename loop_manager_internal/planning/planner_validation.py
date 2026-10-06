"""planning planner validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import execplan_catalog, project_catalog_refresh
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.application.contracts import _FUTURE_IDEA_ITEM
from loop_manager_internal.git.command_adapter import (
    ExternalObservationError, revision_document_or_none, revision_path_exists,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
from loop_manager_internal.planning.planning_catalog import PlanDeletion
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import stat


def _regular_file_or_absent(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _ready_revision_contents(git_bytes: Callable[..., bytes], repository: Path,
                             revision: str, path: str) -> bytes:
    """Keep a missing READY declaration distinct from a failed Git read."""
    try:
        return git_bytes(repository, "show", f"{revision}:{path}")
    except GitLockError:
        raise
    except (RuntimeError, OSError) as error:
        try:
            tree = git_bytes(repository, "ls-tree", "-z", "--full-tree",
                             revision, "--", path)
        except GitLockError:
            raise
        except (RuntimeError, OSError) as tree_error:
            raise ExternalObservationError(
                f"READY ExecPlan revision tree를 읽을 수 없습니다: {path}"
            ) from tree_error
        records = [record for record in tree.split(b"\0") if record]
        if not records:
            raise MaintenancePlanningError(
                f"READY ExecPlan이 revision에 없습니다: {path}"
            ) from error
        if any(record.partition(b"\t")[2] != os.fsencode(path) for record in records):
            raise ExternalObservationError(
                f"READY ExecPlan revision tree 항목이 유효하지 않습니다: {path}"
            ) from error
        raise ExternalObservationError(
            f"READY ExecPlan revision 문서를 읽을 수 없습니다: {path}"
        ) from error


@dataclass(frozen=True)
class FutureIdeaItemsFromTextEffects:
    """Named effects for _future_idea_items_from_text; no entry-point/module access."""
    _idea_tokens: Callable[..., object]


def _future_idea_items_from_text(effects: FutureIdeaItemsFromTextEffects, text: str) -> tuple[tuple[str, ...], ...]:
    items: list[tuple[str, ...]] = []
    for line in text.splitlines():
        match = _FUTURE_IDEA_ITEM.fullmatch(line)
        if match is None:
            continue
        item = re.sub(r"^\[[ xX]\]\s*", "", match.group(1)).strip()
        tokens = effects._idea_tokens(item)
        if tokens:
            items.append(tokens)
    return tuple(items)


@dataclass(frozen=True)
class FutureIdeaItemsEffects:
    """Named effects for _future_idea_items; no entry-point/module access."""
    _future_idea_items_from_text: Callable[..., object]
    git: Callable[..., object]


def _future_idea_items(
    effects: FutureIdeaItemsEffects, worktree: Path,
    active_sha: str,
) -> tuple[tuple[str, ...], ...]:
    path = worktree / "docs/FUTURE_IDEAS.md"
    current_text = ""
    if _regular_file_or_absent(path):
        try:
            current_text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ExternalObservationError("Planner FUTURE_IDEAS를 읽을 수 없습니다.") from error
        except UnicodeError as error:
            raise RuntimeError("Planner FUTURE_IDEAS를 읽을 수 없습니다.") from error

    previous_text = revision_document_or_none(
        effects.git, worktree, active_sha, "docs/FUTURE_IDEAS.md"
    ) or ""
    return tuple(
        dict.fromkeys(
            effects._future_idea_items_from_text(previous_text)
            + effects._future_idea_items_from_text(current_text)
        )
    )


def _contains_token_sequence(
    tokens: tuple[str, ...],
    candidate: tuple[str, ...],
) -> bool:
    width = len(candidate)
    return any(
        tokens[offset : offset + width] == candidate
        for offset in range(len(tokens) - width + 1)
    )


@dataclass(frozen=True)
class ValidateFutureIdeaSeparationEffects:
    """Named effects for _validate_future_idea_separation; no entry-point/module access."""
    _contains_token_sequence: Callable[..., object]
    _future_idea_items: Callable[..., object]
    _idea_tokens: Callable[..., object]


def _validate_future_idea_separation(
    effects: ValidateFutureIdeaSeparationEffects, worktree: Path,
    active_sha: str,
    plan_paths: list[str],
) -> None:
    future_ideas = effects._future_idea_items(worktree, active_sha)
    if not future_ideas:
        return
    for plan_path in plan_paths:
        try:
            plan_text = (worktree / plan_path).read_text(encoding="utf-8")
        except OSError as error:
            raise ExternalObservationError(
                f"Planner ExecPlan을 읽을 수 없습니다: {plan_path}"
            ) from error
        except UnicodeError as error:
            raise RuntimeError(f"Planner ExecPlan을 읽을 수 없습니다: {plan_path}") from error
        plan_tokens = effects._idea_tokens(plan_text)
        if any(
            effects._contains_token_sequence(plan_tokens, idea)
            for idea in future_ideas
        ):
            raise RuntimeError(
                "docs/FUTURE_IDEAS.md 항목을 같은 계획 주기의 새 ExecPlan에 "
                f"포함할 수 없습니다: {plan_path}"
            )


@dataclass(frozen=True)
class ValidatePlannerResultEffects:
    """Named effects for validate_planner_result; no entry-point/module access."""
    _parse_execplan: Callable[..., object]
    _stored_string_list: Callable[..., object]
    _validate_future_idea_separation: Callable[..., object]
    changed_files: Callable[..., object]
    contains_guidance_sections: Callable[..., object]
    file_hash: Callable[..., object]
    git: Callable[..., object]
    load_execplan_catalog: Callable[..., object]
    mutable_snapshot_token: Callable[..., object]


def validate_planner_result(
    effects: ValidatePlannerResultEffects, worktree: Path,
    active_sha: str,
    dirty_files: list[str],
    *,
    connection: sqlite3.Connection | None = None,
    repo: str | None = None,
    mutable_snapshot: MutableReadySnapshot | None = None,
    manager_paths: tuple[str, ...] = (),
) -> ValidatedPlanningResult | None:
    """Validate one create/revise/rename/delete planning batch.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    # A resumed batch can still have Manager moves in the index/worktree.
    # Saved dirty paths are hints, not proof that a receipted move exists.
    observed = (set(effects.changed_files(worktree, active_sha))
                | set(effects.changed_files(worktree)))
    if not set(manager_paths) <= observed:
        raise PlannerValidationError("Manager Requirement 이동 영수증이 실제 변경과 다릅니다.")
    planned_files = sorted((observed | set(dirty_files)) - set(manager_paths))
    if not planned_files:
        return None
    snapshot = mutable_snapshot or MutableReadySnapshot(
        repo=repo or "",
        active_sha=active_sha,
        plans=(),
        token=effects.mutable_snapshot_token(repo or "", active_sha, ()),
    )
    if snapshot.active_sha != active_sha or (repo is not None and snapshot.repo != repo):
        raise PlannerValidationError("mutable READY snapshot이 planning 기준과 다릅니다.")

    plan_paths: list[str] = []
    base_paths: set[str] = set()
    current_plans: dict[str, dict[str, object]] = {}
    for changed_path in planned_files:
        if changed_path == "docs/FUTURE_IDEAS.md":
            if not _regular_file_or_absent(worktree / changed_path):
                raise PlannerValidationError(
                    "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                )
            continue
        if not re.fullmatch(r"docs/plans/[^/]+\.md", changed_path):
            raise PlannerValidationError(
                "Planner는 docs/plans의 새 계획, mutable READY와 "
                "docs/FUTURE_IDEAS.md만 변경할 수 있습니다."
            )
        existed = revision_path_exists(effects.git, worktree, active_sha, changed_path)
        if existed:
            if changed_path not in snapshot.by_path:
                if mutable_snapshot is None:
                    raise PlannerValidationError(
                        "Planner는 기존 ExecPlan을 수정·삭제할 수 없습니다: "
                        f"{changed_path}"
                    )
                raise PlannerValidationError(
                    "Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: "
                    f"{changed_path}"
                )
            base_paths.add(changed_path)
        if not _regular_file_or_absent(worktree / changed_path):
            plan_paths.append(changed_path)
            continue
        try:
            planner_text = (worktree / changed_path).read_text(encoding="utf-8")
            parsed = effects._parse_execplan(
                worktree,
                worktree / changed_path,
                require_priority=not existed,
                allow_archive_fallback=False,
            )
        except (UnicodeError, RuntimeError) as error:
            if isinstance(error, (ExternalObservationError, GitLockError)):
                raise
            raise PlannerValidationError(
                f"Planner ExecPlan을 읽을 수 없습니다: {changed_path} ({error})"
            ) from error
        if effects.contains_guidance_sections(planner_text):
            raise PlannerValidationError("Planner는 AGENTS.md guidance를 제안할 수 없습니다.")
        current_plans[changed_path] = parsed
        plan_paths.append(changed_path)

    try:
        from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
        held_ids = (catalog_held_plan_ids(connection, repo)
                    if connection is not None and repo is not None else frozenset())
        from loop_manager_internal.catalog import remote_deletions
        changed_ids = {str(plan["id"]) for plan in current_plans.values()}
        recovery_ids = {key for raw in snapshot.recovery_goals for key in json.loads(raw)["affected_ids"]} & snapshot.by_id.keys() & changed_ids
        unresolved_holds = held_ids - recovery_ids
        withheld_ids = frozenset()
        if held_ids:
            project_catalog_refresh.assert_planning_dependencies_unheld(
                {}, current_plans.items(), unresolved_holds)
            local = project_catalog_refresh.validated_held_planning_snapshot(
                connection, repo, worktree, active_sha, git=effects.git,
                load_snapshot=execplan_catalog.load_execplan_snapshot,
                mutable_paths=frozenset(plan.path for plan in snapshot.plans if plan.plan_id in recovery_ids),
                recovery_mode=bool(snapshot.recovery_goals))
            catalog = local.active_metadata()
            withheld_ids = local.withheld_ids
        else:
            catalog = effects.load_execplan_catalog(worktree)
        project_catalog_refresh.assert_planning_dependencies_unheld(
            catalog, current_plans.items(), unresolved_holds)
        remote_deletions.validate_recovery(worktree, snapshot, current_plans, catalog)
        plans: list[tuple[str, dict[str, object]]] = []
        revisions: list[PlanRevision] = []
        deletions: list[PlanDeletion] = []
        renamed_ids: set[str] = set()
        displaced_paths: set[str] = set()
        for path in base_paths:
            original = snapshot.by_path.get(path)
            if original is None:
                raise RuntimeError(
                    f"Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: {path}"
                )
            current = current_plans.get(path)
            if current is None or str(current["id"]) != original.plan_id:
                displaced_paths.add(path)

        for path, parsed in current_plans.items():
            plan_id = str(parsed["id"])
            path_identity = snapshot.by_path.get(path)
            if path_identity is not None and plan_id == path_identity.plan_id:
                revisions.append(PlanRevision(path, parsed, path_identity))
                continue
            original = snapshot.by_id.get(plan_id)
            if original is not None:
                if original.path not in displaced_paths or plan_id in renamed_ids:
                    raise RuntimeError(
                        f"ExecPlan rename identity가 모호합니다: {plan_id}"
                    )
                revisions.append(PlanRevision(path, parsed, original))
                renamed_ids.add(plan_id)
            else:
                plans.append((path, parsed))

        for path in sorted(displaced_paths):
            original = snapshot.by_path[path]
            if original.plan_id not in renamed_ids:
                deletions.append(PlanDeletion(original))

        if connection is not None and repo is not None and deletions:
            deleted_ids = {item.snapshot.plan_id for item in deletions}
            revised_ids = {item.snapshot.plan_id for item in revisions}
            for row in connection.execute(
                "SELECT plan_id, depends_on FROM exec_plans WHERE repo = ?",
                (repo,),
            ):
                plan_id = str(row["plan_id"])
                if plan_id in deleted_ids or plan_id in revised_ids:
                    continue
                dependencies = set(
                    effects._stored_string_list(row["depends_on"], "depends_on")
                )
                blocked = sorted(dependencies & deleted_ids)
                if blocked:
                    raise RuntimeError(
                        "삭제 계획을 snapshot 밖 실행 catalog가 참조합니다: "
                        f"{plan_id} -> {', '.join(blocked)}"
                    )

        new_plan_paths = [path for path, _plan in plans]
        effects._validate_future_idea_separation(worktree, active_sha, new_plan_paths)
        new_ids = {str(plan["id"]) for _, plan in plans}
        reserved_ids = (set(catalog) | withheld_ids) - new_ids
        reserved_ids.update(plan.plan_id for plan in snapshot.plans)
        if connection is not None and repo is not None:
            reserved_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT plan_id FROM exec_plans WHERE repo = ? "
                    "UNION SELECT plan_id FROM execplan_cancellations WHERE repo = ?",
                    (repo, repo),
                )
            )
        archived_directory = worktree / "docs/plans/archive"
        if archived_directory.is_dir():
            for archived in archived_directory.glob("*.md"):
                match = re.match(r"\[(G-\d{3,})\]", archived.name)
                if match is not None:
                    reserved_ids.add(match.group(1))
        if new_ids & reserved_ids:
            raise RuntimeError(
                "새 ExecPlan ID가 기존 또는 취소된 ID를 재사용합니다: "
                + ", ".join(sorted(new_ids & reserved_ids))
            )
        existing_ids = [int(plan_id[2:]) for plan_id in reserved_ids]
        first_id = max(existing_ids, default=0) + 1
        expected = list(range(first_id, first_id + len(plans)))
        actual = sorted(int(str(plan["id"])[2:]) for _, plan in plans)
        if actual != expected:
            raise RuntimeError("새 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다.")
        return ValidatedPlanningResult(
            plans=tuple(
                sorted(plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
            publication_hashes=tuple(
                (path, effects.file_hash(worktree / path))
                for path in planned_files
                if _regular_file_or_absent(worktree / path)
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            deletions=tuple(
                sorted(deletions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            mutable_snapshot=snapshot,
        )
    except (ExternalObservationError, GitLockError):
        raise
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


def _stored_string_list(value: object, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise MaintenancePlanningError(
            f"{field} catalog 값이 올바른 JSON 배열이 아닙니다."
        ) from error
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise MaintenancePlanningError(
            f"{field} catalog 값에는 문자열 배열이 필요합니다."
        )
    return tuple(parsed)


@dataclass(frozen=True)
class ReadyPlanSnapshotFromContentsEffects:
    """Named effects for _ready_plan_snapshot_from_contents; no entry-point/module access."""
    _stored_string_list: Callable[..., object]
    is_unowned_ready: Callable[..., object]
    published_execplan: Callable[..., object]


def _ready_plan_snapshot_from_contents(
    effects: ReadyPlanSnapshotFromContentsEffects, row: sqlite3.Row,
    contents: bytes,
) -> ReadyPlanSnapshot:
    plan_id = str(row["plan_id"])
    path = str(row["path"])
    ownership = {
        "branch": row["branch"],
        "worktree": row["worktree"],
        "workspace reservation": int(row["workspace_owned"] or 0),
        "Worker session": row["worker_session_id"],
        "Reviewer session": row["reviewer_session_id"],
        "PR URL": row["pr_url"],
        "PR head": row["pr_head_oid"],
        "PR conflict recovery": int(row["pr_conflict_recovery"] or 0),
        "PR conflict head": row["pr_conflict_head_oid"],
        "PR conflict base": row["pr_conflict_base_oid"],
    }
    owned = [
        name
        for name, value in ownership.items()
        if value is not None and value != 0
    ]
    if not effects.is_unowned_ready(row) or owned:
        detail = ", ".join(owned) if owned else str(row["status"])
        raise MaintenancePlanningError(
            f"유지보수 개정 대상은 무소유 READY 계획이어야 합니다: {plan_id} ({detail})"
        )
    plan_sha = str(row["current_revision_sha"] or row["plan_sha"] or "")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", plan_sha) is None:
        raise MaintenancePlanningError(
            f"계획 revision SHA를 확인할 수 없습니다: {plan_id}"
        )
    content_hash = hashlib.sha256(contents).hexdigest()
    if content_hash != str(row["content_hash"]):
        raise MaintenancePlanningError(
            f"READY ExecPlan content hash가 catalog와 다릅니다: {path}"
        )
    try:
        text = contents.decode("utf-8")
        plan = effects.published_execplan(path, text)
    except (RuntimeError, UnicodeError) as error:
        raise MaintenancePlanningError(str(error)) from error
    sources = effects._stored_string_list(row["sources"], "sources")
    dependencies = effects._stored_string_list(row["depends_on"], "depends_on")
    if (
        re.fullmatch(r"G-\d{3,}", str(plan["id"])) is None
        or str(plan["id"]) != plan_id
        or str(plan["title"]) != str(row["title"])
        or tuple(str(item) for item in plan["sources"]) != sources
        or tuple(str(item) for item in plan["depends_on"]) != dependencies
        or str(plan["priority"]) != str(row["priority"])
    ):
        raise MaintenancePlanningError(
            f"READY ExecPlan metadata가 catalog와 다릅니다: {path}"
        )
    return ReadyPlanSnapshot(
        plan_id=plan_id,
        path=path,
        title=str(plan["title"]),
        sources=sources,
        depends_on=dependencies,
        complexity=str(plan["complexity"]),
        risk=str(plan["risk"]),
        priority=str(plan["priority"]),
        content_hash=content_hash,
        plan_sha=plan_sha,
        text=text,
        current_complexity=row["current_complexity"],
        current_risk=row["current_risk"],
        current_metadata_recorded=True,
    )


@dataclass(frozen=True)
class AssertReadyPlanRevisionEffects:
    """Named effects for _assert_ready_plan_revision; no entry-point/module access."""
    _git_commit_is_ancestor: Callable[..., object]
    git_bytes: Callable[..., object]


def _assert_ready_plan_revision(
    effects: AssertReadyPlanRevisionEffects, repository: Path,
    active_revision: str,
    snapshot: ReadyPlanSnapshot,
    contents: bytes,
) -> None:
    """Bind a READY row to the Git revision that published its exact body."""

    is_ancestor = effects._git_commit_is_ancestor(
        repository, snapshot.plan_sha, active_revision
    )
    revision_contents = _ready_revision_contents(
        effects.git_bytes, repository, snapshot.plan_sha, snapshot.path,
    )
    if not is_ancestor or revision_contents != contents:
        raise MaintenancePlanningError(
            "READY ExecPlan plan revision SHA가 현재 catalog 본문과 "
            f"일치하지 않습니다: {snapshot.plan_id}"
        )


@dataclass(frozen=True)
class GitPathMatchesRevisionEffects:
    """Named effects for _git_path_matches_revision; no entry-point/module access."""
    git: Callable[..., object]


def _git_path_matches_revision(
    effects: GitPathMatchesRevisionEffects, worktree: Path,
    revision: str,
    path: str,
    *,
    cached: bool,
) -> bool:
    """Compare a Git path through its worktree or index checkout filters."""

    arguments = ["diff", "--quiet", "--no-ext-diff"]
    if cached:
        arguments.append("--cached")
    arguments.extend((revision, "--", path))
    result = effects.git(worktree, *arguments, check=False)
    if result.returncode == 1:
        return False
    if result.returncode:
        raise RuntimeError(result.stdout[-6000:].strip())
    return True


@dataclass(frozen=True)
class GitPathHasStandardIndexFlagsEffects:
    """Named effects for _git_path_has_standard_index_flags; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _git_path_has_standard_index_flags(effects: GitPathHasStandardIndexFlagsEffects, worktree: Path, path: str) -> bool:
    """Require a normally tracked index entry before trusting Git diff output."""

    entries = effects.git_bytes(worktree, "ls-files", "-v", "-z", "--", path).split(
        b"\0"
    )
    records = [entry for entry in entries if entry]
    return records == [b"H " + os.fsencode(path)]


@dataclass(frozen=True)
class ReadyPlanSnapshotEffects:
    """Named effects for _ready_plan_snapshot; no entry-point/module access."""
    _assert_ready_plan_revision: Callable[..., object]
    _git_path_has_standard_index_flags: Callable[..., object]
    _git_path_matches_revision: Callable[..., object]
    _ready_plan_snapshot_from_contents: Callable[..., object]
    git_bytes: Callable[..., object]


def _ready_plan_snapshot(
    effects: ReadyPlanSnapshotEffects, worktree: Path,
    active_sha: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    target = worktree / path
    if not _regular_file_or_absent(target):
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일을 확인할 수 없습니다: {path}"
        )
    base_contents = _ready_revision_contents(effects.git_bytes, worktree, active_sha, path)
    standard_index_flags = effects._git_path_has_standard_index_flags(worktree, path)
    checkout_matches = effects._git_path_matches_revision(
        worktree, active_sha, path, cached=False
    )
    if not standard_index_flags or not checkout_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    snapshot = effects._ready_plan_snapshot_from_contents(row, base_contents)
    effects._assert_ready_plan_revision(worktree, active_sha, snapshot, base_contents)
    index_matches = effects._git_path_matches_revision(
        worktree, active_sha, path, cached=True
    )
    if not index_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    return snapshot


@dataclass(frozen=True)
class ReadyPlanSnapshotAtRevisionEffects:
    """Named effects for _ready_plan_snapshot_at_revision; no entry-point/module access."""
    _assert_ready_plan_revision: Callable[..., object]
    _ready_plan_snapshot_from_contents: Callable[..., object]
    git_bytes: Callable[..., object]


def _ready_plan_snapshot_at_revision(
    effects: ReadyPlanSnapshotAtRevisionEffects, repository: Path,
    revision: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    contents = _ready_revision_contents(effects.git_bytes, repository, revision, path)
    snapshot = effects._ready_plan_snapshot_from_contents(row, contents)
    effects._assert_ready_plan_revision(repository, revision, snapshot, contents)
    return snapshot


@dataclass(frozen=True)
class RowIsMutableReadyEffects:
    """Named effects for _row_is_mutable_ready; no entry-point/module access."""
    is_unowned_ready: Callable[..., object]


def _row_is_mutable_ready(effects: RowIsMutableReadyEffects, row: sqlite3.Row) -> bool:
    """Keep the general Planner boundary conservative and evidence based."""

    if not effects.is_unowned_ready(row):
        return False
    if any(row[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS):
        return False
    if any(
        int(row[field] or 0) != 0
        for field in (
            "workspace_owned",
            "retry_count",
            "pr_conflict_recovery",
        )
    ):
        return False
    return all(
        str(row[field] or "{}") == "{}"
        for field in (
            "verify_failure_history",
            "review_failure_history",
            "review_level_history",
        )
    )


@dataclass(frozen=True)
class MutableReadySnapshotEffects:
    """Named effects for mutable_ready_snapshot; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    _ready_plan_snapshot_at_revision: Callable[..., object]
    _row_is_mutable_ready: Callable[..., object]
    mutable_snapshot_token: Callable[..., object]


def mutable_ready_snapshot(
    effects: MutableReadySnapshotEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MutableReadySnapshot:
    """Bind every never-started READY declaration to Git and SQLite bytes."""

    effects._ensure_execplans_schema(connection)
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "활성 maintenance cycle 중에는 일반 mutable READY snapshot을 만들 수 없습니다."
        )
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
        "ORDER BY plan_id",
        (repo,),
    ).fetchall()
    plans: list[ReadyPlanSnapshot] = []
    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    held = catalog_held_plan_ids(connection, repo)
    from loop_manager_internal.catalog import remote_deletions
    recovery = remote_deletions.recoverable_ids(connection, repo, held)
    goals = remote_deletions.recovery_goals(connection, repo)
    for row in rows:
        if (str(row["plan_id"]) in held - recovery or not effects._row_is_mutable_ready(row)):
            continue
        try:
            plans.append(
                effects._ready_plan_snapshot_at_revision(
                    worktree, active_sha, row
                )
            )
        except MaintenancePlanningError as error:
            raise PlannerValidationError(str(error)) from error
    ordered = tuple(sorted(plans, key=lambda item: int(item.plan_id[2:])))
    return MutableReadySnapshot(
        repo=repo,
        active_sha=active_sha,
        plans=ordered,
        token=effects.mutable_snapshot_token(repo, active_sha, ordered, goals),
        recovery_goals=goals,
    )


@dataclass(frozen=True)
class AssertMutableReadySnapshotCurrentEffects:
    """Named effects for _assert_mutable_ready_snapshot_current; no entry-point/module access."""
    _execplan_metadata: Callable[..., object]
    _row_is_mutable_ready: Callable[..., object]


def _assert_mutable_ready_snapshot_current(
    effects: AssertMutableReadySnapshotCurrentEffects, connection: sqlite3.Connection,
    snapshot: MutableReadySnapshot,
) -> None:
    """Compare-and-snapshot guard used immediately before publication."""
    from loop_manager_internal.catalog.remote_deletions import recovery_goals
    if snapshot.recovery_goals != recovery_goals(connection, snapshot.repo):
        raise PlannerValidationError("삭제 목표 감사가 mutable READY snapshot 이후 변경됐습니다.")

    if maintenance_state.MaintenanceRepository(connection).get(
        snapshot.repo
    ).phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "mutable READY snapshot 이후 maintenance cycle이 시작됐습니다."
        )
    for plan in snapshot.plans:
        if not plan.current_metadata_recorded:
            raise PlannerValidationError(
                f"mutable READY snapshot의 SQLite metadata 근거가 없습니다: {plan.plan_id}"
            )
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (snapshot.repo, plan.plan_id),
        ).fetchone()
        if row is None or not effects._row_is_mutable_ready(row):
            raise PlannerValidationError(
                f"mutable READY snapshot 이후 실행 상태가 변경됐습니다: {plan.plan_id}"
            )
        sources, dependencies, priority = effects._execplan_metadata(
            {
                "sources": list(plan.sources),
                "depends_on": list(plan.depends_on),
                "priority": plan.priority,
            }
        )
        current = (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            row["current_complexity"],
            row["current_risk"],
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
        )
        expected = (
            plan.path,
            plan.title,
            sources,
            dependencies,
            priority,
            plan.current_complexity,
            plan.current_risk,
            plan.content_hash,
            plan.plan_sha,
        )
        if current != expected:
            raise PlannerValidationError(
                f"mutable READY snapshot metadata가 변경됐습니다: {plan.plan_id}"
            )
