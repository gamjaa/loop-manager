"""maintenance maintenance publication boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.agents import common_guidance
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import PlanIdAllocation, ValidatedPlanningResult
from loop_manager_internal.catalog import execplan_catalog, project_catalog_refresh
from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
from loop_manager_internal.maintenance import tiered_review
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanMode
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningContext
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningRestart
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import json
import sqlite3


@dataclass(frozen=True)
class ValidateMaintenancePublicationBaseEffects:
    """Named effects for validate_maintenance_publication_base; no entry-point/module access."""
    _assert_maintenance_context_current: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    git: Callable[..., object]


def validate_maintenance_publication_base(
    effects: ValidateMaintenancePublicationBaseEffects, connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    base_branch: str,
    context: MaintenancePlanningContext,
) -> None:
    """Invalidate and restart a stale maintenance result before publication."""

    if context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )

    current = effects.git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if current == context.active_sha:
        return

    cycle_repository = maintenance_state.MaintenanceRepository(connection)
    expected_cycle = cycle_repository.get(repo)
    effects._assert_maintenance_context_current(connection, context)
    with connection:
        restarted = cycle_repository.restart_stale_planning_context(
            repo,
            expected=expected_cycle,
            new_base_sha=current,
        )
        effects._clear_active_in_current_transaction(connection, repo)
    raise MaintenancePlanningRestart(current, restarted.revision)


@dataclass(frozen=True)
class ValidateMaintenancePlanningCommitEffects:
    """Named effects for _validate_maintenance_planning_commit; no entry-point/module access."""
    _parse_execplan: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    load_execplan_catalog: Callable[..., object]
    load_execplan_snapshot: Callable[..., object]


def _validate_maintenance_planning_commit(
    effects: ValidateMaintenancePlanningCommitEffects, repository: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *, connection: sqlite3.Connection | None = None,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    head = effects.git(repository, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise MaintenancePlanningError(
            "maintenance planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = effects.git(
        repository,
        "rev-list",
        "--parents",
        "-n",
        "1",
        plan_commit,
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != context.active_sha:
        raise MaintenancePlanningError(
            "maintenance batch는 평가 기준 바로 다음의 단일 planning commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            line
            for line in effects.git(
                repository,
                "diff",
                "--name-only",
                context.active_sha,
                plan_commit,
            ).stdout.splitlines()
            if line
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise MaintenancePlanningError(
            "maintenance planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    validated_hashes = dict(result.publication_hashes)
    if (
        len(validated_hashes) != len(result.publication_hashes)
        or set(validated_hashes) != set(result.publication_paths)
    ):
        raise MaintenancePlanningError(
            "maintenance planning 결과의 검증된 file hash가 불완전합니다."
        )
    expected = {
        path: plan for path, plan in result.plans
    }
    expected.update({revision.path: revision.plan for revision in result.revisions})
    held_ids = (
        catalog_held_plan_ids(connection, context.repo)
        if connection is not None else frozenset()
    )
    if held_ids:
        try:
            snapshot = project_catalog_refresh.validated_held_planning_snapshot(
                connection, context.repo, repository, context.active_sha,
                git=effects.git, load_snapshot=effects.load_execplan_snapshot)
            project_catalog_refresh.assert_planning_dependencies_unheld(
                snapshot.active_metadata(), expected.items(), held_ids)
        except execplan_catalog.ExecPlanCatalogError as error:
            raise MaintenancePlanningError(str(error)) from error
    else:
        effects.load_execplan_catalog(repository, validate_sources=False)
    for path in result.publication_paths:
        local_contents = (repository / path).read_bytes()
        remote_contents = effects.git_bytes(repository, "show", f"{plan_commit}:{path}")
        expected_hash = validated_hashes[path]
        if (
            hashlib.sha256(local_contents).hexdigest() != expected_hash
            or hashlib.sha256(remote_contents).hexdigest() != expected_hash
        ):
            raise MaintenancePlanningError(
                "maintenance ExecPlan이 Planner 결과 검증 후 "
                f"변경됐습니다: {path}"
            )
        if path == ARTIFACT_PATH:
            if result.maintenance_code_map_hash != expected_hash:
                raise MaintenancePlanningError("Manager가 검증한 코드맵 출력이 아닙니다.")
            continue
        if path == "docs/FUTURE_IDEAS.md":
            continue
        parsed = effects._parse_execplan(repository, repository / path, validate_sources=False)
        if parsed != expected[path]:
            raise MaintenancePlanningError(
                f"검증 뒤 maintenance ExecPlan metadata가 변경됐습니다: {path}"
            )


@dataclass(frozen=True)
class ValidateMaintenancePublicationCommitEffects:
    """Named effects for _validate_maintenance_publication_commit; no entry-point/module access."""
    _validate_maintenance_planning_commit: Callable[..., object]
    git: Callable[..., object]


def _validate_maintenance_publication_commit(
    effects: ValidateMaintenancePublicationCommitEffects, repository: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *,
    require_remote_confirmation: bool = True,
    connection: sqlite3.Connection | None = None,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    remote = effects.git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    expected_remote = (
        plan_commit if require_remote_confirmation else context.active_sha
    )
    if remote != expected_remote:
        detail = "fast-forward push" if require_remote_confirmation else "평가 기준"
        raise MaintenancePlanningError(
            f"maintenance planning commit의 {detail}을 origin/base에서 "
            "확인할 수 없습니다."
        )
    effects._validate_maintenance_planning_commit(
        repository, result, plan_commit, connection=connection,
    )


@dataclass(frozen=True)
class GitCommitIsAncestorEffects:
    """Named effects for _git_commit_is_ancestor; no entry-point/module access."""
    git: Callable[..., object]


def _git_commit_is_ancestor(
    effects: GitCommitIsAncestorEffects, repository: Path,
    older: str,
    newer: str,
) -> bool:
    result = effects.git(
        repository, "merge-base", "--is-ancestor", older, newer, check=False
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("maintenance publication Git ancestry 확인 실패.")
    return result.returncode == 0


@dataclass(frozen=True)
class AssertRemotePreservesMaintenanceBatchEffects:
    """Named effects for _assert_remote_preserves_maintenance_batch; no entry-point/module access."""
    _git_commit_is_ancestor: Callable[..., object]
    _git_tree_entries: Callable[..., object]


def _assert_remote_preserves_maintenance_batch(
    effects: AssertRemotePreservesMaintenanceBatchEffects, repository: Path,
    base_sha: str,
    plan_commit: str,
    remote_sha: str,
) -> None:
    """Confirm a published batch remains byte-identical at a newer remote tip."""

    if not effects._git_commit_is_ancestor(repository, plan_commit, remote_sha):
        raise MaintenancePlanningError(
            "보존된 maintenance planning commit이 origin/base 이력에 없습니다."
        )
    base_tree = effects._git_tree_entries(repository, base_sha)
    plan_tree = effects._git_tree_entries(repository, plan_commit)
    remote_tree = effects._git_tree_entries(repository, remote_sha)
    batch_paths = {
        path
        for path in base_tree.keys() | plan_tree.keys()
        if base_tree.get(path) != plan_tree.get(path)
    }
    changed_paths = sorted(
        path
        for path in batch_paths
        if plan_tree.get(path) != remote_tree.get(path)
    )
    if changed_paths:
        rendered = ", ".join(
            path.decode("utf-8", errors="replace") for path in changed_paths
        )
        raise MaintenancePlanningError(
            "게시된 maintenance batch 경로가 후속 원격 commit에서 변경됐습니다: "
            f"{rendered}"
        )

@dataclass(frozen=True)
class RecordMaintenancePublicationIntentEffects:
    """Named effects for record_maintenance_publication_intent; no entry-point/module access."""
    _assert_maintenance_context_current: Callable[..., object]
    _validate_maintenance_publication_commit: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def record_maintenance_publication_intent(
    effects: RecordMaintenancePublicationIntentEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Persist a validated local batch before attempting its remote push."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    effects._assert_maintenance_context_current(connection, context)
    effects._validate_maintenance_publication_commit(
        worktree,
        base_branch,
        result,
        plan_commit,
        require_remote_confirmation=False,
        connection=connection,
    )
    expected_phase = (
        maintenance_state.MaintenancePhase.ASSESSMENT
        if context.mode is MaintenancePlanMode.BARRIER
        else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
    )
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    evidence = None
    if (expected_phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and ((cycle.file_assessment_start_merged_count is not None
              and (not effects.is_loop_manager_repository(Path(repo)) or cycle.full_assessment_completed))
             or tiered_review.has_assessment(connection, repo, context.cycle_revision, context.active_sha))):
        evidence = maintenance_file_publication.collect(
            connection, repo, worktree, base_branch,
            context.cycle_revision, context.active_sha,
        )
    with runtime_state._transaction(connection):
        intent = maintenance_state.MaintenanceRepository(
            connection
        ).record_publication_intent(
            repo,
            expected_phase=expected_phase,
            expected_revision=context.cycle_revision,
            base_sha=context.active_sha,
            commit_sha=plan_commit,
            refactoring_plan_ids=result.refactoring_plan_ids,
            revalidation_plan_ids=result.revalidation_plan_ids,
            id_allocations=tuple(
                (item.source_path, item.source_id, item.target_path, item.target_id)
                for item in result.id_allocations
            ),
        )
        if evidence is not None:
            maintenance_file_publication.save_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit, evidence,
            )
        return intent


@dataclass(frozen=True)
class MaintenanceRowsMatchPublishedBatchEffects:
    """Named effects for _maintenance_rows_match_published_batch; no entry-point/module access."""
    _execplan_metadata: Callable[..., object]
    file_hash: Callable[..., object]


def _maintenance_rows_match_published_batch(
    effects: MaintenanceRowsMatchPublishedBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    for path, plan in result.plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, str(plan["id"])),
        ).fetchone()
        sources, dependencies, priority = effects._execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            effects.file_hash(worktree / path),
            plan_commit,
            "READY",
        ):
            return False
    for revision in result.revisions:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
        ).fetchone()
        sources, dependencies, priority = effects._execplan_metadata(dict(revision.plan))
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            revision.path,
            revision.snapshot.title,
            sources,
            dependencies,
            priority,
            effects.file_hash(worktree / revision.path),
            plan_commit,
            "READY",
        ):
            return False
    return True


def _assert_maintenance_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    context: MaintenancePlanningContext,
    plan_commit: str,
    result: ValidatedPlanningResult,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    result_id_allocations = tuple(
        (item.source_path, item.source_id, item.target_path, item.target_id)
        for item in result.id_allocations
    )
    if not cycle.publication_id_allocations and all(
        source_path == target_path and source_id == target_id
        for source_path, source_id, target_path, target_id in result_id_allocations
    ):
        # Pre-mapping prepared intents migrate with []; treat identity allocations
        # as the legacy equivalent while still binding non-identity mappings.
        result_id_allocations = ()
    current = (
        cycle.publication_stage,
        cycle.publication_base_sha,
        cycle.publication_commit_sha,
        cycle.publication_remote_oid,
        cycle.publication_refactoring_plan_ids,
        cycle.publication_revalidation_plan_ids,
        cycle.publication_id_allocations,
    )
    expected = (
        "prepared", context.active_sha, plan_commit, None,
        result.refactoring_plan_ids, result.revalidation_plan_ids,
        result_id_allocations,
    )
    if current != expected:
        raise MaintenancePlanningError(
            "원격 push 전에 보존한 maintenance publication intent가 "
            "현재 batch와 일치하지 않습니다."
        )


@dataclass(frozen=True)
class MaintenancePublicationAlreadyAppliedEffects:
    """Named effects for _maintenance_publication_already_applied; no entry-point/module access."""
    _maintenance_rows_match_published_batch: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def _maintenance_publication_already_applied(
    effects: MaintenancePublicationAlreadyAppliedEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    context = result.maintenance_context
    assert context is not None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if context.mode is MaintenancePlanMode.BARRIER and (result.refactoring_plan_ids or cycle.assessment_plan_ids or cycle.prerequisite_plan_ids):
        cycle_matches = (
            cycle.phase is (maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
                            if cycle.prerequisite_plan_ids and not result.refactoring_plan_ids
                            else maintenance_state.MaintenancePhase.REFACTORING)
            and cycle.revision == context.cycle_revision + 1
            and cycle.refactoring_plan_ids == result.refactoring_plan_ids
            and cycle.revalidation_plan_ids == result.revalidation_plan_ids
            and cycle.publication_base_sha == context.active_sha
            and cycle.publication_commit_sha == plan_commit
            and cycle.publication_remote_oid == plan_commit
        )
    else:
        cycle_matches = (
            cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and cycle.revision == context.cycle_revision + 1
            and cycle.last_cycle_commit == plan_commit
        )
    if (cycle_matches and context.mode is MaintenancePlanMode.BARRIER
        and ((cycle.file_assessment_start_merged_count is not None
              and (not effects.is_loop_manager_repository(Path(repo)) or cycle.full_assessment_completed))
             or tiered_review.has_assessment(connection, repo, context.cycle_revision, context.active_sha))):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, context.cycle_revision, context.active_sha,
            plan_commit,
        )
        if evidence.tiered is not None:
            success = tiered_review.TieredReviewRepository(connection).success(repo)
            if success is None or success["digest"] != evidence.tiered["digest"]:
                return False
        else:
            success = maintenance_file_ledger.FileReviewRepository(connection).last_success(repo)
            if (success is None or success.source_revision != evidence.inventory.revision
                or cycle.last_success_file_assessment_merged_count is None):
                return False
    return cycle_matches and effects._maintenance_rows_match_published_batch(
        connection, repo, worktree, result, plan_commit
    )


@dataclass(frozen=True)
class PublishMaintenancePlanningBatchEffects:
    """Named effects for publish_maintenance_planning_batch; no entry-point/module access."""
    _apply_maintenance_planning_batch: Callable[..., object]
    _validate_maintenance_publication_commit: Callable[..., object]


def publish_maintenance_planning_batch(
    effects: PublishMaintenancePlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Atomically publish catalog revisions after a confirmed remote push.

    Calling this again after a process interruption is idempotent when the
    remote commit, catalog rows, and persisted cycle describe the same batch.
    """

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    effects._validate_maintenance_publication_commit(
        worktree, base_branch, result, plan_commit, connection=connection
    )
    return effects._apply_maintenance_planning_batch(
        connection, repo, worktree, base_branch, result, plan_commit
    )


@dataclass(frozen=True)
class ApplyMaintenancePlanningBatchEffects:
    """Named effects for _apply_maintenance_planning_batch; no entry-point/module access."""
    _assert_maintenance_context_current: Callable[..., object]
    _assert_maintenance_publication_intent: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _execplan_metadata: Callable[..., object]
    _maintenance_publication_already_applied: Callable[..., object]
    _planning_input_fingerprint: Callable[..., object]
    plan_declarations: Callable[..., object]
    _register_execplan_rows: Callable[..., object]
    _specification_input_fingerprint: Callable[..., object]
    file_hash: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def _apply_maintenance_planning_batch(
    effects: ApplyMaintenancePlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *, current_remote: str | None = None,
) -> maintenance_state.MaintenanceCycle:
    """Apply one already-confirmed maintenance batch in a SQLite transaction."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    with runtime_state._transaction(connection):
        repository = maintenance_state.MaintenanceRepository(connection)
        if effects._maintenance_publication_already_applied(
            connection, repo, worktree, result, plan_commit
        ):
            return repository.get(repo)
        include_general_planning = repository.get(repo).include_general_planning
        effects._assert_maintenance_publication_intent(
            connection, repo, context, plan_commit, result
        )
        effects._assert_maintenance_context_current(connection, context)
        effects._register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        from loop_manager_internal.notifications import notifications
        notifications.record_planning_publication(connection, repo, result.plans, plan_commit)
        for revision in result.revisions:
            snapshot = revision.snapshot
            _sources, dependencies, _priority = effects._execplan_metadata(
                dict(revision.plan)
            )
            cursor = connection.execute(
                """
                UPDATE exec_plans
                SET depends_on = ?, content_hash = ?, plan_sha = ?,
                    current_revision_sha = ?, current_complexity = ?, current_risk = ?,
                    catalog_generation = catalog_generation + 1
                WHERE repo = ? AND plan_id = ? AND status = 'READY'
                  AND path = ? AND title = ? AND sources = ?
                  AND depends_on = ? AND priority = ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ?
                  AND branch IS NULL AND worktree IS NULL
                  AND workspace_owned = 0
                  AND worker_session_id IS NULL
                  AND reviewer_session_id IS NULL AND pr_url IS NULL
                  AND pr_head_oid IS NULL
                  AND pr_conflict_recovery = 0
                  AND pr_conflict_head_oid IS NULL
                  AND pr_conflict_base_oid IS NULL
                """,
                (
                    dependencies,
                    effects.file_hash(worktree / revision.path),
                    plan_commit,
                    plan_commit,
                    str(revision.plan["complexity"]),
                    str(revision.plan["risk"]),
                    repo,
                    snapshot.plan_id,
                    snapshot.path,
                    snapshot.title,
                    json.dumps(
                        snapshot.sources,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        snapshot.depends_on,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    snapshot.priority,
                    snapshot.content_hash,
                    snapshot.plan_sha,
                ),
            )
            if cursor.rowcount != 1:
                raise MaintenancePlanningError(
                    "publication 직전 READY snapshot이 변경됐습니다: "
                    f"{snapshot.plan_id}"
                )
            from loop_manager_internal.storage import execplan_draft_state
            from loop_manager_internal.catalog import execplan_drafts
            execplan_draft_state.observe(connection, repo, revision.path, revision.plan,
                effects.file_hash(worktree / revision.path), plan_commit, agent=True,
                inputs=execplan_drafts.input_digests(worktree, revision.plan))

        cycle = repository.get(repo)
        if (context.mode is MaintenancePlanMode.BARRIER
            and ((cycle.file_assessment_start_merged_count is not None
                  and (not effects.is_loop_manager_repository(Path(repo)) or cycle.full_assessment_completed))
                 or tiered_review.has_assessment(connection, repo, context.cycle_revision, context.active_sha))):
            evidence = maintenance_file_publication.saved_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit,
            )
            maintenance_file_publication.promote(
                connection, repo, Path(repo), base_branch,
                current_remote or plan_commit, evidence,
            )
            if evidence.tiered is None:
                repository.advance_file_assessment_success_baseline(
                    repo, expected_revision=context.cycle_revision,
                    source_sha=context.active_sha,
                    expected_merged_count=evidence.merged_count,
                    bump_revision=False,
                )

        if (
            context.mode is MaintenancePlanMode.BARRIER
            and (result.refactoring_plan_ids or cycle.assessment_plan_ids or cycle.prerequisite_plan_ids)
        ):
            next_cycle = repository.transition(
                repo,
                expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=context.cycle_revision,
                next_phase=(maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
                            if cycle.prerequisite_plan_ids and not result.refactoring_plan_ids
                            else maintenance_state.MaintenancePhase.REFACTORING),
                refactoring_plan_ids=result.refactoring_plan_ids,
                revalidation_plan_ids=result.revalidation_plan_ids,
                publication_stage="remote_confirmed",
                publication_base_sha=context.active_sha,
                publication_commit_sha=plan_commit,
                publication_remote_oid=plan_commit,
                include_general_planning=False,
            )
        else:
            next_cycle = repository.advance_success_baseline(
                repo,
                expected_phase=(
                    maintenance_state.MaintenancePhase.ASSESSMENT
                    if context.mode is MaintenancePlanMode.BARRIER
                    else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
                ),
                expected_revision=context.cycle_revision,
                remote_commit=plan_commit,
            )
        guidance_hash = common_guidance.effective_planning_hash(connection, repo)
        from loop_manager_internal.storage import planning_input_state
        after = effects.plan_declarations(worktree, plan_commit)
        if include_general_planning:
            planning_input_state.record_general(connection, repo, plan_commit, after)
        effects._clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit if include_general_planning else None,
            specification_fingerprint=(
                effects._specification_input_fingerprint(worktree, plan_commit, guidance_hash=guidance_hash)
                if include_general_planning
                else None
            ),
            planning_fingerprint=(
                effects._planning_input_fingerprint(worktree, plan_commit, guidance_hash=guidance_hash)
                if include_general_planning
                else None
            ),
        )
        if not include_general_planning:
            before = effects.plan_declarations(worktree, context.active_sha)
            planning_input_state.advance_verified_delta(
                connection, repo, plan_commit,
                planning_input_state.delta(before, after),
            )
        return next_cycle


@dataclass(frozen=True)
class RestartStaleMaintenancePublicationEffects:
    """Named effects for restart_stale_maintenance_publication; no entry-point/module access."""
    _clear_active_in_current_transaction: Callable[..., object]
    _git_commit_is_ancestor: Callable[..., object]
    _git_tree_entries: Callable[..., object]
    git: Callable[..., object]


def restart_stale_maintenance_publication(
    effects: RestartStaleMaintenancePublicationEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Restart a batch proven absent from the newly fetched remote base.

    Git files and refs remain intact. The caller creates a fresh planning
    worktree at cycle_base_sha and obtains a new context after this returns.
    """
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    if (
        cycle.publication_stage != "prepared"
        or not cycle.publication_base_sha
        or not cycle.publication_commit_sha
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError("재시작할 prepared publication intent가 없습니다.")
    remote = effects.git(
        worktree, "rev-parse", "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    base = cycle.publication_base_sha
    commit = cycle.publication_commit_sha
    if remote in {base, commit}:
        raise MaintenancePlanningError("게시 기준이 이동하지 않았거나 batch가 이미 게시됐습니다.")

    if (
        not effects._git_commit_is_ancestor(worktree, base, remote)
        or effects._git_commit_is_ancestor(worktree, commit, remote)
    ):
        raise MaintenancePlanningError("미게시 batch임을 확정할 수 없어 재시작할 수 없습니다.")
    base_tree = effects._git_tree_entries(worktree, base)
    commit_tree = effects._git_tree_entries(worktree, commit)
    remote_tree = effects._git_tree_entries(worktree, remote)
    batch_paths = {
        path for path in base_tree.keys() | commit_tree.keys()
        if base_tree.get(path) != commit_tree.get(path)
    }
    if any(base_tree.get(path) != remote_tree.get(path) for path in batch_paths):
        raise MaintenancePlanningError("원격 파일이 보존된 batch와 겹쳐 재시작할 수 없습니다.")
    with runtime_state._transaction(connection):
        restarted = repository.restart_prepared_batch(
            repo, expected=cycle, new_base_sha=remote
        )
        effects._clear_active_in_current_transaction(connection, repo)
        return restarted


@dataclass(frozen=True)
class RecoverMaintenancePlanningBatchEffects:
    """Named effects for recover_maintenance_planning_batch; no entry-point/module access."""
    _apply_maintenance_planning_batch: Callable[..., object]
    _assert_remote_preserves_maintenance_batch: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _git_commit_is_ancestor: Callable[..., object]
    _maintenance_context_at_revision: Callable[..., object]
    _validate_maintenance_planning_commit: Callable[..., object]
    changed_files: Callable[..., object]
    git: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    publish_maintenance_planning_batch: Callable[..., object]
    restart_stale_maintenance_publication: Callable[..., object]
    validate_maintenance_planner_result: Callable[..., object]


def recover_maintenance_planning_batch(
    effects: RecoverMaintenancePlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Recover a pushed batch from its SQLite intent and remote commit."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.publication_stage == "remote_confirmed":
        remote_oid = cycle.publication_remote_oid
        current_remote = effects.git(
            worktree,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        if remote_oid and current_remote == remote_oid:
            return cycle
        if (
            not remote_oid
            or not cycle.publication_base_sha
            or cycle.publication_commit_sha != remote_oid
        ):
            raise MaintenancePlanningError(
                "적용된 maintenance publication 기록이 완전하지 않습니다."
            )
        effects._assert_remote_preserves_maintenance_batch(
            worktree,
            cycle.publication_base_sha,
            remote_oid,
            current_remote,
        )
        return cycle
    if (
        cycle.publication_stage != "prepared"
        or cycle.publication_base_sha is None
        or cycle.publication_commit_sha is None
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError(
            "복구할 maintenance publication intent가 없습니다."
        )
    base_sha = cycle.publication_base_sha
    plan_commit = cycle.publication_commit_sha
    current_remote = effects.git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    remote_advanced_after_publication = False
    if current_remote != plan_commit:
        if current_remote == base_sha:
            raise MaintenancePlanningError(
                "보존된 maintenance planning commit의 push가 아직 확인되지 않았습니다."
            )
        if effects._git_commit_is_ancestor(worktree, plan_commit, current_remote):
            effects._assert_remote_preserves_maintenance_batch(
                worktree, base_sha, plan_commit, current_remote,
            )
            remote_advanced_after_publication = True
        else:
            return effects.restart_stale_maintenance_publication(
                connection, repo, worktree, base_branch
            )
    if (
        remote_advanced_after_publication
        and cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and ((cycle.file_assessment_start_merged_count is not None
              and (not effects.is_loop_manager_repository(Path(repo)) or cycle.full_assessment_completed))
             or tiered_review.has_assessment(connection, repo, cycle.revision, base_sha))
    ):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, cycle.revision, base_sha, plan_commit,
        )
        if not maintenance_file_publication.sources_match_remote(
            Path(repo), base_branch, current_remote, evidence,
        ):
            # The plans remain published. A later catalog reconciliation imports
            # them, while this assessment must inspect the new source revision.
            with runtime_state._transaction(connection):
                restarted = maintenance_state.MaintenanceRepository(
                    connection
                ).restart_prepared_batch(
                    repo, expected=cycle, new_base_sha=current_remote,
                )
                effects._clear_active_in_current_transaction(connection, repo)
                return restarted
    context = effects._maintenance_context_at_revision(
        connection, repo, worktree, base_sha, cycle
    )
    try:
        result = effects.validate_maintenance_planner_result(
            connection,
            context,
            worktree,
            effects.changed_files(worktree),
            refactoring_plan_ids=(cycle.publication_refactoring_plan_ids if context.mode is MaintenancePlanMode.BARRIER else ()),
            allow_general_plans=cycle.include_general_planning,
            expected_id_allocations=(
                tuple(PlanIdAllocation(*item) for item in cycle.publication_id_allocations)
                or None
            ),
        )
    except PlannerValidationError as error:
        raise MaintenancePlanningError(
            f"보존된 maintenance publication batch가 유효하지 않습니다: {error}"
        ) from error
    if result is None:
        raise MaintenancePlanningError(
            "보존된 maintenance publication commit에 검증할 batch가 없습니다."
        )
    if remote_advanced_after_publication:
        effects._validate_maintenance_planning_commit(
            worktree, result, plan_commit, connection=connection,
        )
        return effects._apply_maintenance_planning_batch(
            connection, repo, worktree, base_branch, result, plan_commit,
            current_remote=current_remote,
        )
    return effects.publish_maintenance_planning_batch(
        connection,
        repo,
        worktree,
        base_branch,
        result,
        plan_commit,
    )
