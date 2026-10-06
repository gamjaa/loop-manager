"""maintenance maintenance validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.catalog import project_catalog_refresh, execplan_drafts
from loop_manager_internal.git.command_adapter import ExternalObservationError
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
from loop_manager_internal.maintenance.maintenance_file_inventory import AssessmentError
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanMode
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningContext
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage import maintenance_state
from pathlib import Path
import hashlib
import json
import re
import sqlite3


@dataclass(frozen=True)
class MaintenanceModeAndRowsEffects:
    """Named effects for _maintenance_mode_and_rows; no entry-point/module access."""
    is_unowned_ready: Callable[..., object]


def _maintenance_mode_and_rows(
    effects: MaintenanceModeAndRowsEffects, connection: sqlite3.Connection,
    repo: str,
    cycle: maintenance_state.MaintenanceCycle,
    active_sha: str,
) -> tuple[MaintenancePlanMode, list[sqlite3.Row]]:
    held_ids = catalog_held_plan_ids(connection, repo)
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        if cycle.cycle_base_sha != active_sha:
            raise MaintenancePlanningError(
                "유지보수 assessment 기준 SHA가 planning worktree 기준과 "
                "일치하지 않습니다."
            )
        rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
        return MaintenancePlanMode.BARRIER, [row for row in rows
            if row["plan_id"] not in held_ids and effects.is_unowned_ready(row)]
    if cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        rows = []
        for plan_id in cycle.revalidation_plan_ids:
            if plan_id in held_ids:
                raise MaintenancePlanningError(f"재검증 대상 계획이 catalog 보류 중입니다: {plan_id}")
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
                "WHERE h.repo = exec_plans.repo "
                "AND h.currently_archived = 1 "
                "AND h.plan_id = exec_plans.plan_id)",
                (repo, plan_id),
            ).fetchone()
            if row is None:
                raise MaintenancePlanningError(
                    f"재검증 대상 계획이 catalog에 없습니다: {plan_id}"
                )
            rows.append(row)
        return MaintenancePlanMode.REVALIDATION, rows
    raise MaintenancePlanningError(
        "활성 유지보수 assessment 또는 post-refactor revalidation 단계가 "
        "없어 기존 ExecPlan 수정 권한을 열 수 없습니다."
    )


def _maintenance_context(
    repo: str,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
    mode: MaintenancePlanMode,
    snapshots: tuple[ReadyPlanSnapshot, ...],
) -> MaintenancePlanningContext:
    return MaintenancePlanningContext(
        repo=repo,
        mode=mode,
        active_sha=active_sha,
        cycle_revision=cycle.revision,
        cycle_phase=cycle.phase.value,
        eligible_plans=snapshots,
    )


@dataclass(frozen=True)
class PrepareMaintenancePlanningContextEffects:
    """Named effects for prepare_maintenance_planning_context; no entry-point/module access."""
    _maintenance_context: Callable[..., object]
    _maintenance_mode_and_rows: Callable[..., object]
    _ready_plan_snapshot: Callable[..., object]
    _ready_plan_snapshot_at_revision: Callable[..., object]


def prepare_maintenance_planning_context(
    effects: PrepareMaintenancePlanningContextEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MaintenancePlanningContext:
    """Bind guarded Planner authority to one active SQLite cycle revision."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    mode, rows = effects._maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    planning = connection.execute(
        "SELECT planner_session_id, agent_snapshot, "
        "planning_result_pending_validation, planning_result_completed, "
        "planning_validation_error, planning_retry_count "
        "FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    resumed = planning is not None and any(planning)
    # A resumed Planner may already have edited an eligible READY file. Its
    # authority is still the original Git blob and SQLite row; the changed
    # checkout is validated separately as the Planner result.
    snapshot_at = (
        effects._ready_plan_snapshot_at_revision if resumed
        else effects._ready_plan_snapshot
    )
    snapshots = tuple(snapshot_at(worktree, active_sha, row) for row in rows)
    return effects._maintenance_context(repo, active_sha, cycle, mode, snapshots)


@dataclass(frozen=True)
class MaintenanceContextAtRevisionEffects:
    """Named effects for _maintenance_context_at_revision; no entry-point/module access."""
    _maintenance_context: Callable[..., object]
    _maintenance_mode_and_rows: Callable[..., object]
    _ready_plan_snapshot_at_revision: Callable[..., object]


def _maintenance_context_at_revision(
    effects: MaintenanceContextAtRevisionEffects, connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> MaintenancePlanningContext:
    mode, rows = effects._maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    snapshots = tuple(
        effects._ready_plan_snapshot_at_revision(repository, active_sha, row)
        for row in rows
    )
    return effects._maintenance_context(repo, active_sha, cycle, mode, snapshots)


@dataclass(frozen=True)
class AssertMaintenanceContextCurrentEffects:
    """Named effects for _assert_maintenance_context_current; no entry-point/module access."""
    _maintenance_mode_and_rows: Callable[..., object]
    _stored_string_list: Callable[..., object]
    is_unowned_ready: Callable[..., object]


def _assert_maintenance_context_current(
    effects: AssertMaintenanceContextCurrentEffects, connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(context.repo)
    if cycle.phase.value != context.cycle_phase or cycle.revision != context.cycle_revision:
        raise MaintenancePlanningError(
            "유지보수 주기 단계 또는 revision이 Planner 실행 뒤 변경됐습니다."
        )
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        expected_mode = MaintenancePlanMode.BARRIER
        _mode, rows = effects._maintenance_mode_and_rows(
            connection, context.repo, cycle, context.active_sha
        )
        expected_plan_ids = tuple(str(row["plan_id"]) for row in rows)
    elif cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        expected_mode = MaintenancePlanMode.REVALIDATION
        expected_plan_ids = cycle.revalidation_plan_ids
    else:
        raise MaintenancePlanningError(
            "현재 유지보수 단계에는 기존 ExecPlan 수정 권한이 없습니다."
        )
    context_plan_ids = tuple(plan.plan_id for plan in context.eligible_plans)
    if context.mode is not expected_mode or context_plan_ids != expected_plan_ids:
        raise MaintenancePlanningError(
            "maintenance mode 또는 eligible READY snapshot이 SQLite 단계와 "
            "일치하지 않습니다."
        )
    held_ids = catalog_held_plan_ids(connection, context.repo)
    for snapshot in context.eligible_plans:
        if snapshot.plan_id in held_ids:
            raise MaintenancePlanningError(f"READY snapshot 계획이 catalog 보류 중입니다: {snapshot.plan_id}")
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (context.repo, snapshot.plan_id),
        ).fetchone()
        if row is None:
            raise MaintenancePlanningError(
                f"READY snapshot 계획이 catalog에서 사라졌습니다: {snapshot.plan_id}"
            )
        if not effects.is_unowned_ready(row):
            raise MaintenancePlanningError(
                f"READY snapshot의 실행 이력 또는 소유권이 변경됐습니다: {snapshot.plan_id}"
            )
        ownership_values = (
            row["branch"],
            row["worktree"],
            int(row["workspace_owned"] or 0),
            row["worker_session_id"],
            row["reviewer_session_id"],
            row["pr_url"],
            row["pr_head_oid"],
            int(row["pr_conflict_recovery"] or 0),
            row["pr_conflict_head_oid"],
            row["pr_conflict_base_oid"],
        )
        current = (
            str(row["status"]),
            str(row["path"]),
            str(row["title"]),
            effects._stored_string_list(row["sources"], "sources"),
            effects._stored_string_list(row["depends_on"], "depends_on"),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            ownership_values,
        )
        expected = (
            "READY",
            snapshot.path,
            snapshot.title,
            snapshot.sources,
            snapshot.depends_on,
            snapshot.priority,
            snapshot.content_hash,
            snapshot.plan_sha,
            (None, None, 0, None, None, None, None, 0, None, None),
        )
        if current != expected:
            raise MaintenancePlanningError(
                "READY snapshot의 상태, metadata 또는 소유권이 변경됐습니다: "
                f"{snapshot.plan_id}"
            )


@dataclass(frozen=True)
class ValidateMaintenancePlannerResultEffects:
    """Named effects for validate_maintenance_planner_result; no entry-point/module access."""
    _assert_maintenance_context_current: Callable[..., object]
    _git_tree_entries: Callable[..., object]
    _parse_execplan: Callable[..., object]
    _validate_future_idea_separation: Callable[..., object]
    changed_files: Callable[..., object]
    contains_guidance_sections: Callable[..., object]
    git: Callable[..., object]
    load_execplan_snapshot: Callable[..., object]
    published_execplan: Callable[..., object]
    terminal_new_plan_ids: Callable[..., object]
    validate_barrier_revision: Callable[..., object]
    validate_refactoring_plan: Callable[..., object]
    validate_revalidation_revision: Callable[..., object]


def validate_maintenance_planner_result(
    effects: ValidateMaintenancePlannerResultEffects, connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
    worktree: Path,
    dirty_files: list[str],
    *,
    refactoring_plan_ids: tuple[str, ...] | None = None,
    allow_general_plans: bool = False,
) -> ValidatedPlanningResult | None:
    """Validate output without consuming correction retries on Git failures."""

    # Read Git outside the output-validation boundary. A tree query distinguishes
    # absent paths from an unreadable repository without interpreting cat-file's
    # nonzero status as proof that a path is new.
    from loop_manager_internal.maintenance import code_map_artifact
    planned_files = sorted(
        set(dirty_files) | set(effects.changed_files(worktree, context.active_sha))
    )
    base_paths = effects._git_tree_entries(worktree, context.active_sha)
    path_moves_by_sha: dict[str, dict[str, str]] = {}
    if context.mode is MaintenancePlanMode.REVALIDATION:
        for snapshot in context.eligible_plans:
            if snapshot.path not in planned_files:
                continue
            if snapshot.plan_sha not in path_moves_by_sha:
                tree = effects._git_tree_entries(worktree, snapshot.plan_sha)
                # Only unique, byte-identical deleted -> added files establish
                # a move. Similarity, copies and ambiguous replacements cannot
                # prove preservation of the original acceptance criterion.
                old_blobs: dict[bytes, list[bytes]] = {}
                new_blobs: dict[bytes, list[bytes]] = {}
                for entries, grouped in ((tree, old_blobs), (base_paths, new_blobs)):
                    for name, (kind, oid) in entries.items():
                        if kind == b"blob":
                            grouped.setdefault(oid, []).append(name)
                moves = {}
                for oid, old_names in old_blobs.items():
                    new_names = new_blobs.get(oid, [])
                    if len(old_names) == len(new_names) == 1:
                        old, new = old_names[0], new_names[0]
                        if old not in base_paths and new not in tree:
                            moves[old.decode("utf-8")] = new.decode("utf-8")
                path_moves_by_sha[snapshot.plan_sha] = moves
    try:
        effects._assert_maintenance_context_current(connection, context)
        artifact_result = None
        if code_map_artifact.ARTIFACT_PATH in planned_files:
            artifact_result = code_map_artifact.attach_candidate(
                connection, context, worktree, None, allow_stale=True,
            )
            planned_files.remove(code_map_artifact.ARTIFACT_PATH)
        if not planned_files:
            return artifact_result
        eligible = context.eligible_by_path
        planned_contents: dict[str, tuple[bytes, str]] = {}
        new_paths: list[str] = []
        revised_paths: list[str] = []
        for path in planned_files:
            if path == "docs/FUTURE_IDEAS.md" and allow_general_plans:
                if not (worktree / path).is_file():
                    raise MaintenancePlanningError(
                        "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                    )
                planned_contents[path] = ((worktree / path).read_bytes(), "")
                continue
            if not re.fullmatch(r"docs/plans/[^/]+\.md", path):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 검증된 docs/plans batch만 변경할 수 있습니다."
                )
            if not (worktree / path).is_file():
                raise MaintenancePlanningError(
                    f"유지보수 Planner는 ExecPlan을 삭제·rename할 수 없습니다: {path}"
                )
            contents = (worktree / path).read_bytes()
            text = contents.decode("utf-8")
            planned_contents[path] = (contents, text)
            if effects.contains_guidance_sections(text):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 AGENTS.md guidance를 제안할 수 없습니다."
                )
            existed = path.encode("utf-8") in base_paths
            if existed:
                if path not in eligible:
                    raise MaintenancePlanningError(
                        f"SQLite snapshot에 없는 기존 ExecPlan은 수정할 수 없습니다: {path}"
                    )
                revised_paths.append(path)
            else:
                if context.mode is not MaintenancePlanMode.BARRIER:
                    raise MaintenancePlanningError(
                        "post-refactor revalidation은 새 ExecPlan을 만들 수 없습니다."
                    )
                new_paths.append(path)

        try:
            held_ids = catalog_held_plan_ids(connection, context.repo)
            if held_ids:
                catalog_snapshot = project_catalog_refresh.validated_held_planning_snapshot(
                    connection, context.repo, worktree, context.active_sha,
                    git=effects.git, load_snapshot=effects.load_execplan_snapshot)
            else:
                catalog_snapshot = effects.load_execplan_snapshot(worktree, validate_sources=False)
        except (ExternalObservationError, GitLockError):
            raise
        except RuntimeError as error:
            raise MaintenancePlanningError(str(error)) from error
        catalog = catalog_snapshot.active_metadata()
        new_plans: list[tuple[str, dict[str, object]]] = []
        for path in new_paths:
            try:
                plan = effects._parse_execplan(
                    worktree,
                    worktree / path,
                    require_priority=True,
                    allow_archive_fallback=False,
                )
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            try:
                validated_plan = effects.published_execplan(path, planned_contents[path][1])
                execplan_drafts.validate_agent_change(validated_plan)
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            new_plans.append((path, validated_plan))
        try:
            from loop_manager_internal.storage import execplan_draft_state
            execplan_draft_state.validate_questions(connection, context.repo, catalog)
            project_catalog_refresh.assert_planning_dependencies_unheld(
                catalog, new_plans + [(path, catalog[eligible[path].plan_id]) for path in revised_paths],
                held_ids)
        except RuntimeError as error:
            raise MaintenancePlanningError(str(error)) from error
        new_ids = {str(plan["id"]) for _, plan in new_plans}
        declared_refactoring = (
            new_ids
            if refactoring_plan_ids is None
            else set(refactoring_plan_ids)
        )
        if not declared_refactoring <= new_ids:
            unknown = sorted(declared_refactoring - new_ids)
            raise MaintenancePlanningError(
                "MAINTENANCE_RESULT가 생성되지 않은 refactoring plan을 참조합니다: "
                + ", ".join(unknown)
            )
        general_ids = new_ids - declared_refactoring
        if general_ids and not allow_general_plans:
            raise MaintenancePlanningError(
                "maintenance-only 호출은 일반 기능 ExecPlan을 만들 수 없습니다: "
                + ", ".join(sorted(general_ids))
            )
        for path, plan in new_plans:
            if str(plan["id"]) in declared_refactoring:
                effects.validate_refactoring_plan(path, plan, planned_contents[path][1])
        if allow_general_plans:
            effects._validate_future_idea_separation(worktree, context.active_sha, new_paths)

        existing_ids = [
            int(plan_id[2:])
            for plan_id in catalog_snapshot.observed_ids
            if plan_id not in new_ids
        ]
        first_id = max(existing_ids, default=0) + 1
        expected_ids = list(range(first_id, first_id + len(new_plans)))
        actual_ids = sorted(int(str(plan["id"])[2:]) for _, plan in new_plans)
        if actual_ids != expected_ids:
            raise MaintenancePlanningError(
                "새 리팩터링 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다."
            )

        refactoring_plans = [
            item for item in new_plans
            if str(item[1]["id"]) in declared_refactoring
        ]
        terminals = effects.terminal_new_plan_ids(refactoring_plans)
        if context.mode is MaintenancePlanMode.BARRIER:
            if refactoring_plans:
                from loop_manager_internal.maintenance.maintenance_orchestration import (
                    dependency_plan_ids, validate_prerequisite_plan_ids, MaintenanceOrchestrationError,
                )
                rows = maintenance_state.MaintenanceRepository(connection).plan_rows(context.repo)
                statuses = {str(row["plan_id"]): str(row["status"]) for row in rows}
                external = {dependency for _path, plan in refactoring_plans for dependency in plan["depends_on"]
                            if dependency not in declared_refactoring and statuses.get(dependency) != "MERGED"}
                try:
                    required = {plan_id for plan_id in dependency_plan_ids(rows, external) if statuses[plan_id] != "MERGED"}
                    validate_prerequisite_plan_ids(rows, tuple(required))
                except MaintenanceOrchestrationError as error:
                    raise MaintenancePlanningError(str(error)) from error
                barrier_paths = {path for path, snapshot in eligible.items() if snapshot.plan_id not in required}
                if set(revised_paths) != barrier_paths:
                    missing = sorted(barrier_paths - set(revised_paths))
                    raise MaintenancePlanningError(
                        "barrier mode는 필요한 선행을 제외한 모든 eligible READY 계획에 말단 dependency를 "
                        f"추가해야 합니다: {', '.join(missing)}"
                    )
                for _path, plan in new_plans:
                    if str(plan["id"]) in general_ids and not set(terminals) <= set(plan["depends_on"]):
                        raise MaintenancePlanningError(
                            "combined 일반 계획은 리팩터링 DAG 말단 뒤에 있어야 합니다: "
                            + str(plan["id"])
                        )
            elif revised_paths:
                raise MaintenancePlanningError(
                    "리팩터링 계획이 없는 assessment는 READY 장벽을 수정할 수 없습니다."
                )

        revisions: list[PlanRevision] = []
        for path in revised_paths:
            snapshot = eligible[path]
            plan = catalog[snapshot.plan_id]
            text = planned_contents[path][1]
            try:
                validated_plan = effects.published_execplan(path, text)
                execplan_drafts.validate_agent_change(validated_plan, snapshot.text)
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            if context.mode is MaintenancePlanMode.BARRIER:
                effects.validate_barrier_revision(
                    snapshot, path, validated_plan, text, terminals
                )
            else:
                effects.validate_revalidation_revision(
                    snapshot, path, validated_plan, text,
                    path_moves=path_moves_by_sha[snapshot.plan_sha],
                )
            revisions.append(PlanRevision(path, validated_plan, snapshot))

        result = ValidatedPlanningResult(
            plans=tuple(
                sorted(new_plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
            publication_hashes=tuple(
                (path, hashlib.sha256(planned_contents[path][0]).hexdigest())
                for path in planned_files
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            maintenance_context=context,
            refactoring_plan_ids=tuple(
                sorted(declared_refactoring, key=lambda value: int(value[2:]))
            ),
            revalidation_plan_ids=(
                tuple(
                    sorted(
                        {revision.snapshot.plan_id for revision in revisions}
                        | general_ids,
                        key=lambda value: int(value[2:]),
                    )
                ) if refactoring_plans else ()
            ),
        )
        return (code_map_artifact.attach_candidate(connection, context, worktree, result)
                if artifact_result is not None else result)
    except (MaintenancePlanningError, code_map_artifact.CodeMapArtifactError) as error:
        raise PlannerValidationError(str(error)) from error
    except (OSError, UnicodeError) as error:
        raise PlannerValidationError(str(error)) from error


def _plan_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", path)
    return match.group(1) if match else None


RESULT_PREFIX = "FULL_ASSESSMENT_RESULT: "
_PLAN_ID = re.compile(r"G-\d{3,}")
_PRODUCT_DOCS = {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}


def parse_assessment_result(
    output: str, inventory: dict[str, str], revision: str,
    new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
) -> str:
    """Validate evidence and return canonical JSON suitable for SQLite."""
    lines = [line[len(RESULT_PREFIX):] for line in output.splitlines()
             if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > 65536:
        raise AssessmentError("전수 평가 결과 JSON 줄이 하나 필요하며 64 KiB를 넘을 수 없습니다.")
    try:
        report = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise AssessmentError("전수 평가 결과가 올바른 JSON이 아닙니다.") from error
    if not isinstance(report, dict) or set(report) != {"source_revision", "coverage", "findings"}:
        raise AssessmentError("전수 평가 결과 필드가 올바르지 않습니다.")
    if report["source_revision"] != revision:
        raise AssessmentError("전수 평가 결과의 source revision이 다릅니다.")
    coverage = report["coverage"]
    if (not isinstance(coverage, dict) or set(coverage) != {"code", "tests", "documents"}
        or any(not isinstance(value, str) or len(value.strip()) < 12 for value in coverage.values())):
        raise AssessmentError("코드·테스트·문서 평가 근거가 각각 필요합니다.")
    findings = report["findings"]
    if not isinstance(findings, list) or len(findings) > 150:
        raise AssessmentError("전수 평가 발견 목록은 최대 150개입니다.")
    seen: set[str] = set()
    linked_new: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"id", "kind", "path", "evidence", "status", "plan_ids", "source_hash"}:
            raise AssessmentError("발견 항목의 필드가 올바르지 않습니다.")
        identifier, kind, path, evidence, status, plan_ids, source_hash = (
            finding[key] for key in ("id", "kind", "path", "evidence", "status", "plan_ids", "source_hash")
        )
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9-]{3,64}", identifier)
            or identifier in seen
            or not isinstance(kind, str) or kind not in {"code", "document"}
            or not isinstance(path, str) or path not in inventory
            or source_hash != inventory[path]
            or not isinstance(evidence, str) or len(evidence.strip()) < 20
            or not isinstance(status, str)
            or not isinstance(plan_ids, list)
            or any(not isinstance(item, str) or _PLAN_ID.fullmatch(item) is None for item in plan_ids)
            or len(plan_ids) != len(set(plan_ids))):
            raise AssessmentError("발견 항목의 근거·경로·원본 지문이 유효하지 않습니다.")
        seen.add(identifier)
        if kind == "document":
            if path not in _PRODUCT_DOCS and not path.startswith("docs/requirements/"):
                raise AssessmentError("문서 발견 대상이 활성 제품 문서가 아닙니다.")
            if status not in {"candidate", "decision_required", "no_issue"} or plan_ids:
                raise AssessmentError("문서 발견은 수정본 후보·결정 대상·문제 없음으로만 기록합니다.")
        else:
            if status not in {"planned", "existing_plan", "resolved", "decision_required", "no_issue"}:
                raise AssessmentError("코드 발견 상태가 유효하지 않습니다.")
            if status in {"planned", "existing_plan"} and not plan_ids:
                raise AssessmentError("코드 개선 발견에는 연결된 계획 ID가 필요합니다.")
            if status in {"resolved", "decision_required", "no_issue"} and plan_ids:
                raise AssessmentError("계획 없는 코드 발견 상태에 plan ID가 있습니다.")
            if status == "planned":
                linked_new.update(plan_ids)
            if status == "existing_plan" and not set(plan_ids) <= known_plan_ids:
                raise AssessmentError("기존 계획으로 연결한 ID가 catalog에 없습니다.")
    if linked_new != set(new_plan_ids):
        raise AssessmentError("새 리팩터링 계획과 코드 발견의 연결이 일치하지 않습니다.")
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_saved_assessment_result(
    saved_result: str, inventory: dict[str, str], revision: str,
) -> str:
    """Read an earlier serialized report against the same immutable source."""
    try:
        report = json.loads(saved_result)
    except (TypeError, json.JSONDecodeError) as error:
        raise AssessmentError("저장된 전수 평가 결과가 올바른 JSON이 아닙니다.") from error
    if not isinstance(report, dict) or not isinstance(report.get("findings"), list):
        raise AssessmentError("저장된 전수 평가 발견 목록이 유효하지 않습니다.")
    planned: set[str] = set()
    existing: set[str] = set()
    for finding in report["findings"]:
        if not isinstance(finding, dict) or not isinstance(finding.get("plan_ids"), list):
            raise AssessmentError("저장된 전수 평가 발견 항목이 유효하지 않습니다.")
        if finding.get("status") == "planned":
            planned.update(item for item in finding["plan_ids"] if isinstance(item, str))
        elif finding.get("status") == "existing_plan":
            existing.update(item for item in finding["plan_ids"] if isinstance(item, str))
    return parse_assessment_result(
        RESULT_PREFIX + saved_result, inventory, revision,
        tuple(sorted(planned)), existing,
    )
