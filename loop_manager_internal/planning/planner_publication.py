"""planning planner publication boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.documents import document_publication
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
from loop_manager_internal.planning.planning_catalog import PlanDeletion
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import planning_publication_state, planning_input_state, planning_review_state
from loop_manager_internal.storage import runtime_state, work_claims
from loop_manager_internal.storage import execplan_draft_state
from loop_manager_internal.catalog import execplan_drafts
from loop_manager_internal.storage.planning_publication_state import GeneralPlanningPublication
from pathlib import Path
import hashlib
import json
import sqlite3


@dataclass(frozen=True)
class RegisterExecplanRowsEffects:
    """Named effects for _register_execplan_rows; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    file_hash: Callable[..., object]
    git: Callable[..., object]


def _register_execplan_rows(
    effects: RegisterExecplanRowsEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
) -> None:
    """Apply the new-row part shared by normal and maintenance batches."""

    effects._ensure_execplans_schema(connection)
    remote_url = None
    if any(plan.get("draft") for _path, plan in plans):
        from loop_manager_internal.notifications.rendering import _checkpoint_link
        origin = effects.git(worktree, "remote", "get-url", "origin").stdout.strip()
        link = _checkpoint_link(origin, plan_commit)
        remote_url = link.split("/tree/", 1)[0] if link else None
    for path, plan in plans:
        execplan_catalog_state.register_published_plan_row(
            connection, repo, path, plan, effects.file_hash(worktree / path), plan_commit,
        )
        if plan.get("draft"):
            execplan_draft_state.observe(connection, repo, path, plan,
                effects.file_hash(worktree / path), plan_commit, inputs=execplan_drafts.input_digests(worktree, plan),
                remote_url=remote_url, agent=True)


@dataclass(frozen=True)
class RegisterExecplansEffects:
    """Named effects for register_execplans; no entry-point/module access."""
    _register_execplan_rows: Callable[..., object]


def register_execplans(
    effects: RegisterExecplansEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
    planning_fingerprint: str,
) -> None:
    """Register one published Planner batch atomically and idempotently."""

    with connection:
        effects._register_execplan_rows(connection, repo, worktree, plans, plan_commit)
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=plan_commit,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )


@dataclass(frozen=True)
class PlanningResultManifestEffects:
    """Named effects for _planning_result_manifest; no entry-point/module access."""
    snapshot_to_dict: Callable[..., object]


def _planning_result_manifest(effects: PlanningResultManifestEffects, result: ValidatedPlanningResult) -> str:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError(
            "일반 planning batch에 mutable READY snapshot이 없습니다."
        )
    payload = {
        "version": 1,
        "publication_paths": list(result.publication_paths),
        "publication_hashes": dict(result.publication_hashes),
        "retirement_moves": list(result.retirement_moves),
        "restoration_moves": list(result.restoration_moves),
        "plans": [[path, plan] for path, plan in result.plans],
        "revisions": [
            {
                "path": revision.path,
                "plan": dict(revision.plan),
                "snapshot": effects.snapshot_to_dict(revision.snapshot),
            }
            for revision in result.revisions
        ],
        "deletions": [
            effects.snapshot_to_dict(deletion.snapshot)
            for deletion in result.deletions
        ],
        "mutable_snapshot": {
            "repo": snapshot.repo,
            "active_sha": snapshot.active_sha,
            "token": snapshot.token,
            "plans": [effects.snapshot_to_dict(plan) for plan in snapshot.plans],
            "recovery_goals": list(snapshot.recovery_goals),
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class PlanningResultFromManifestEffects:
    """Named effects for _planning_result_from_manifest; no entry-point/module access."""
    mutable_snapshot_token: Callable[..., object]
    snapshot_from_dict: Callable[..., object]


def _planning_result_from_manifest(effects: PlanningResultFromManifestEffects, manifest: str) -> ValidatedPlanningResult:
    try:
        payload = json.loads(manifest)
        if payload.get("version") != 1:
            raise ValueError("unsupported version")
        snapshot_value = payload["mutable_snapshot"]
        snapshot_plans = tuple(
            effects.snapshot_from_dict(item) for item in snapshot_value["plans"]
        )
        snapshot = MutableReadySnapshot(
            repo=str(snapshot_value["repo"]),
            active_sha=str(snapshot_value["active_sha"]),
            plans=snapshot_plans,
            token=str(snapshot_value["token"]),
            recovery_goals=tuple(snapshot_value.get("recovery_goals", ())),
        )
        expected_token = effects.mutable_snapshot_token(
            snapshot.repo, snapshot.active_sha, snapshot.plans, snapshot.recovery_goals
        )
        if snapshot.token != expected_token:
            raise ValueError("snapshot token mismatch")
        plans = tuple(
            (str(item[0]), dict(item[1])) for item in payload["plans"]
        )
        revisions = tuple(
            PlanRevision(
                str(item["path"]),
                dict(item["plan"]),
                effects.snapshot_from_dict(item["snapshot"]),
            )
            for item in payload["revisions"]
        )
        deletions = tuple(
            PlanDeletion(effects.snapshot_from_dict(item))
            for item in payload["deletions"]
        )
        publication_hashes = tuple(
            sorted(
                (str(path), str(value))
                for path, value in payload["publication_hashes"].items()
            )
        )
        return ValidatedPlanningResult(
            plans=plans,
            publication_paths=tuple(
                str(path) for path in payload["publication_paths"]
            ),
            publication_hashes=publication_hashes,
            retirement_moves=tuple(str(path) for path in payload.get("retirement_moves", ())),
            restoration_moves=tuple(str(path) for path in payload.get("restoration_moves", ())),
            revisions=revisions,
            deletions=deletions,
            mutable_snapshot=snapshot,
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise PlannerValidationError(
            "보존된 general planning publication manifest가 유효하지 않습니다."
        ) from error


@dataclass(frozen=True)
class ValidateGeneralPlanningCommitEffects:
    """Named effects for _validate_general_planning_commit; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]


def _validate_general_planning_commit(
    effects: ValidateGeneralPlanningCommitEffects, worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError("일반 planning snapshot이 없습니다.")
    head = effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise PlannerValidationError(
            "general planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = effects.git(
        worktree, "rev-list", "--parents", "-n", "1", plan_commit
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != snapshot.active_sha:
        raise PlannerValidationError(
            "general planning batch는 시작 기준 바로 다음 단일 commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            path
            for path in effects._nul_git_paths(
                worktree,
                "diff",
                "--no-renames",
                "--name-only",
                snapshot.active_sha,
                plan_commit,
                "-z",
            )
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise PlannerValidationError(
            "general planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    hashes = dict(result.publication_hashes)
    if len(hashes) != len(result.publication_hashes):
        raise PlannerValidationError("general planning file hash가 중복됐습니다.")
    expected_existing = {
        path
        for path in result.publication_paths
        if effects.git(
            worktree,
            "cat-file",
            "-e",
            f"{plan_commit}:{path}",
            check=False,
        ).returncode
        == 0
    }
    if set(hashes) != expected_existing:
        raise PlannerValidationError(
            "general planning 결과의 file hash 또는 삭제 manifest가 불완전합니다."
        )
    for path, expected_hash in hashes.items():
        contents = effects.git_bytes(worktree, "show", f"{plan_commit}:{path}")
        if hashlib.sha256(contents).hexdigest() != expected_hash:
            raise PlannerValidationError(
                f"general planning 파일이 검증 후 변경됐습니다: {path}"
            )
    for path in set(result.publication_paths) - expected_existing:
        if effects.git(
            worktree, "cat-file", "-e", f"{plan_commit}:{path}", check=False
        ).returncode == 0:
            raise PlannerValidationError(
                f"삭제 manifest 경로가 planning commit에 남아 있습니다: {path}"
            )
    for source in result.retirement_moves:
        if not source.startswith("docs/requirements/") or source.count("/") != 2:
            raise PlannerValidationError(f"Requirement 이동 원본이 유효하지 않습니다: {source}")
        destination = "docs/requirements/archive/" + source.removeprefix("docs/requirements/")
        if source not in result.publication_paths or destination not in result.publication_paths:
            raise PlannerValidationError(f"Requirement 이동 manifest가 불완전합니다: {source}")
        if effects.git(worktree, "cat-file", "-e", f"{plan_commit}:{source}", check=False).returncode == 0:
            raise PlannerValidationError(f"Requirement 원본이 planning commit에 남아 있습니다: {source}")
        if effects.git(worktree, "cat-file", "-e", f"{snapshot.active_sha}:{destination}", check=False).returncode == 0:
            raise PlannerValidationError(f"Requirement 이동 목적지가 이미 있었습니다: {destination}")
        original = effects.git_bytes(worktree, "show", f"{snapshot.active_sha}:{source}")
        moved = effects.git_bytes(worktree, "show", f"{plan_commit}:{destination}")
        if moved != original:
            raise PlannerValidationError(f"Requirement 이동 중 원문이 변경됐습니다: {source}")
    for archived in result.restoration_moves:
        if not archived.startswith("docs/requirements/archive/") or archived.count("/") != 3:
            raise PlannerValidationError(f"Requirement 복원 원본이 유효하지 않습니다: {archived}")
        destination = "docs/requirements/" + archived.removeprefix("docs/requirements/archive/")
        if archived not in result.publication_paths or destination not in result.publication_paths:
            raise PlannerValidationError(f"Requirement 복원 manifest가 불완전합니다: {archived}")
        if effects.git(worktree, "cat-file", "-e", f"{plan_commit}:{archived}", check=False).returncode == 0:
            raise PlannerValidationError(f"Requirement archive 원본이 planning commit에 남아 있습니다: {archived}")
        if effects.git(worktree, "cat-file", "-e", f"{snapshot.active_sha}:{destination}", check=False).returncode == 0:
            raise PlannerValidationError(f"Requirement 복원 목적지가 이미 있었습니다: {destination}")
        original = effects.git_bytes(worktree, "show", f"{snapshot.active_sha}:{archived}")
        restored = effects.git_bytes(worktree, "show", f"{plan_commit}:{destination}")
        if restored != original:
            raise PlannerValidationError(f"Requirement 복원 중 원문이 변경됐습니다: {archived}")


@dataclass(frozen=True)
class RecordGeneralPlanningPublicationIntentEffects:
    """Named effects for record_general_planning_publication_intent; no entry-point/module access."""
    _assert_mutable_ready_snapshot_current: Callable[..., object]
    _planning_result_manifest: Callable[..., object]
    _validate_general_planning_commit: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def record_general_planning_publication_intent(
    effects: RecordGeneralPlanningPublicationIntentEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> GeneralPlanningPublication:
    with runtime_state._transaction(connection):
        snapshot = result.mutable_snapshot
        if snapshot is None or snapshot.repo != repo:
            raise PlannerValidationError(
                "SQLite-bound mutable READY snapshot이 publication 대상과 다릅니다."
            )
        effects._assert_mutable_ready_snapshot_current(connection, work_claims.affected_snapshot(result))
        work_claims.assert_manual_scopes_available(connection, repo, result)
        work_claims.assert_new_ids_available(connection, repo, (plan["id"] for _path, plan in result.plans))
        effects._validate_general_planning_commit(worktree, result, plan_commit)
        if effects.is_loop_manager_repository(Path(repo)):
            from loop_manager_internal.documents import document_drafts
            project = connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            if project is None:
                raise PlannerValidationError("문서 계획의 등록 프로젝트가 없습니다.")
            drafts = document_drafts.DocumentDraftRepository(connection)
            try:
                for path, plan in result.plans:
                    plan_text = document_publication.read_plan_text(worktree / path)
                    claim = document_publication.plan_claim(plan_text)
                    if claim is not None:
                        try:
                            draft = drafts.get(claim)
                            document_publication.validate_document_plan(plan_text, plan, draft)
                            drafts.assert_current(draft, Path(repo), str(project["base_branch"]))
                        except document_drafts.DocumentDraftError as error:
                            drafts.record_publication_error(claim, str(error))
                            raise
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                raise PlannerValidationError(str(error)) from error
        manifest = effects._planning_result_manifest(result)
        manifest_hash = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
        return planning_publication_state.record_general_planning_publication(
            connection, GeneralPlanningPublication(
                repo, "prepared", snapshot.active_sha, plan_commit, snapshot.token,
                specification_fingerprint, planning_fingerprint, manifest, manifest_hash,
            ),
        )


@dataclass(frozen=True)
class GeneralPlanningBatchAlreadyAppliedEffects:
    """Named effects for _general_planning_batch_already_applied; no entry-point/module access."""
    _execplan_metadata: Callable[..., object]
    file_hash: Callable[..., object]
    plan_declarations: Callable[..., object]


def _general_planning_batch_already_applied(
    effects: GeneralPlanningBatchAlreadyAppliedEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> bool:
    project = connection.execute(
        "SELECT processed_sha, specification_fingerprint, planning_fingerprint "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or tuple(project) != (
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    ):
        return False
    if not planning_input_state.matches(
        connection, repo, effects.plan_declarations(worktree, plan_commit),
    ):
        return False
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
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            effects.file_hash(worktree / path),
            plan_commit,
        ):
            return False
    for revision in result.revisions:
        plan = dict(revision.plan)
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
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
            bool(row["ever_started"]),
        ) != (
            revision.path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            effects.file_hash(worktree / revision.path),
            plan_commit,
            "READY",
            False,
        ):
            return False
    for deletion in result.deletions:
        original = deletion.snapshot
        row = connection.execute(
            "SELECT * FROM execplan_cancellations "
            "WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone()
        if row is None or (
            str(row["path"]),
            str(row["content_hash"]),
            str(row["plan_sha"]),
            str(row["deletion_commit"]),
        ) != (
            original.path,
            original.content_hash,
            original.plan_sha,
            plan_commit,
        ):
            return False
        if connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone() is not None:
            return False
    return True


@dataclass(frozen=True)
class ApplyGeneralPlanningBatchEffects:
    """Named effects for _apply_general_planning_batch; no entry-point/module access."""
    _assert_mutable_ready_snapshot_current: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _general_planning_batch_already_applied: Callable[..., object]
    _register_execplan_rows: Callable[..., object]
    file_hash: Callable[..., object]
    general_planning_publication: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    plan_declarations: Callable[..., object]


def _apply_general_planning_batch(
    effects: ApplyGeneralPlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None or snapshot.repo != repo:
        raise PlannerValidationError("general planning snapshot 대상이 다릅니다.")
    with runtime_state._transaction(connection):
        intent = effects.general_planning_publication(connection, repo)
        if intent is None:
            if effects._general_planning_batch_already_applied(
                connection,
                repo,
                worktree,
                result,
                plan_commit,
                specification_fingerprint,
                planning_fingerprint,
            ):
                return
            raise PlannerValidationError(
                "보존된 general planning publication intent가 없습니다."
            )
        if (
            intent.stage,
            intent.base_sha,
            intent.commit_sha,
            intent.snapshot_token,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        ) != (
            "prepared",
            snapshot.active_sha,
            plan_commit,
            snapshot.token,
            specification_fingerprint,
            planning_fingerprint,
        ):
            raise PlannerValidationError(
                "보존된 general planning publication intent가 batch와 다릅니다."
            )
        effects._assert_mutable_ready_snapshot_current(connection, work_claims.affected_snapshot(result))
        planning_publication_state.apply_mutable_catalog_changes(
            connection, repo, deletions=result.deletions, revisions=result.revisions,
            snapshot_token=snapshot.token, plan_commit=plan_commit,
            content_hash=lambda path: effects.file_hash(worktree / path),
            draft_inputs=lambda plan: execplan_drafts.input_digests(worktree, plan),
        )
        effects._register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        from loop_manager_internal.catalog.remote_deletions import resolve_recovery
        resolve_recovery(connection, snapshot, plan_commit,
                         {item.snapshot.plan_id for item in result.revisions})
        if effects.is_loop_manager_repository(Path(repo)):
            from loop_manager_internal.documents import document_drafts
            drafts = document_drafts.DocumentDraftRepository(connection)
            try:
                for path, plan in result.plans:
                    plan_text = document_publication.read_plan_text(worktree / path)
                    claim = document_publication.plan_claim(plan_text)
                    if claim is not None:
                        draft = drafts.get(claim)
                        document_publication.validate_document_plan(plan_text, plan, draft)
                        drafts.bind_plan(repo, claim, str(plan["id"]))
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                raise PlannerValidationError(str(error)) from error
        planning_review_state.confirm(connection, repo, snapshot.active_sha, plan_commit)
        planning_input_state.record_general(
            connection, repo, plan_commit,
            effects.plan_declarations(worktree, plan_commit),
        )
        effects._clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )
        for source in result.retirement_moves:
            changed = connection.execute(
                "UPDATE requirement_move_receipts SET stage = 'published', "
                "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND source_path = ? "
                "AND base_sha = ? AND stage = 'pending'",
                (repo, source, snapshot.active_sha),
            )
            if changed.rowcount != 1:
                raise PlannerValidationError(
                    f"Requirement 이동 영수증을 확인할 수 없습니다: {source}"
                )
            connection.execute(
                "UPDATE requirement_audit SET stage = 'published', "
                "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND source_path = ?",
                (repo, source),
            )
        for archived in result.restoration_moves:
            source = "docs/requirements/" + archived.removeprefix("docs/requirements/archive/")
            changed = connection.execute(
                "UPDATE requirement_audit SET stage = 'published', updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND source_path = ? AND observed_sha = ? "
                "AND outcome = 'restore' AND stage = 'staged'",
                (repo, source, snapshot.active_sha),
            )
            if changed.rowcount != 1:
                raise PlannerValidationError(
                    f"Requirement 복원 영수증을 확인할 수 없습니다: {source}"
                )
        planning_publication_state.delete_general_planning_publication(connection, repo)


@dataclass(frozen=True)
class PublishGeneralPlanningBatchEffects:
    """Named effects for publish_general_planning_batch; no entry-point/module access."""
    _apply_general_planning_batch: Callable[..., object]
    _validate_general_planning_commit: Callable[..., object]
    git: Callable[..., object]


def publish_general_planning_batch(
    effects: PublishGeneralPlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    remote = effects.git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote != plan_commit:
        raise PlannerValidationError(
            "general planning commit의 fast-forward push를 origin/base에서 확인할 수 없습니다."
        )
    effects._validate_general_planning_commit(worktree, result, plan_commit)
    effects._apply_general_planning_batch(
        connection,
        repo,
        worktree,
        result,
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    )


@dataclass(frozen=True)
class RecoverGeneralPlanningBatchEffects:
    """Named effects for recover_general_planning_batch; no entry-point/module access."""
    _apply_general_planning_batch: Callable[..., object]
    _assert_remote_preserves_maintenance_batch: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _git_commit_is_ancestor: Callable[..., object]
    _planning_result_from_manifest: Callable[..., object]
    _validate_general_planning_commit: Callable[..., object]
    general_planning_publication: Callable[..., object]
    git: Callable[..., object]


def recover_general_planning_batch(
    effects: RecoverGeneralPlanningBatchEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> str:
    """Return pending, applied, or restart for one durable general intent."""

    intent = effects.general_planning_publication(connection, repo)
    if intent is None:
        raise PlannerValidationError(
            "복구할 general planning publication intent가 없습니다."
        )
    result = effects._planning_result_from_manifest(intent.manifest)
    if result.mutable_snapshot is None or (
        result.mutable_snapshot.token != intent.snapshot_token
    ):
        raise PlannerValidationError(
            "publication intent와 mutable READY snapshot token이 다릅니다."
        )
    effects._validate_general_planning_commit(worktree, result, intent.commit_sha)
    remote = effects.git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote == intent.base_sha:
        return "pending"
    if effects._git_commit_is_ancestor(worktree, intent.commit_sha, remote):
        if remote != intent.commit_sha:
            effects._assert_remote_preserves_maintenance_batch(
                worktree,
                intent.base_sha,
                intent.commit_sha,
                remote,
            )
        effects._apply_general_planning_batch(
            connection,
            repo,
            worktree,
            result,
            intent.commit_sha,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        )
        return "applied"
    if not effects._git_commit_is_ancestor(worktree, intent.base_sha, remote):
        raise PlannerValidationError(
            "general planning publication base가 origin/base 이력에 없습니다."
        )
    with runtime_state._transaction(connection):
        planning_publication_state.delete_general_planning_publication(connection, repo)
        effects._clear_active_in_current_transaction(connection, repo)
    return "restart"
