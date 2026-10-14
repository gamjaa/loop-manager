"""execution plan validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ExecPlanMetadataError
from loop_manager_internal.catalog import execplan_catalog, requirement_sources
from loop_manager_internal.execution import execution_revision
from loop_manager_internal.git.command_adapter import (
    ExternalObservationError, revision_document_or_none,
    revision_requirement_documents,
)
from loop_manager_internal.runtime.contracts import GitLockError
from pathlib import Path
import base64
import hashlib
import os
import tempfile
import json
import sqlite3
import stat


def _execplan_input_fingerprint(entries: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-blocked-input-v1\0")
    for relative_path, contents in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if contents is None:
            digest.update(b"\0")
            continue
        normalized = contents.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        digest.update(b"\1")
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


@dataclass(frozen=True)
class WorktreeExecplanInputFingerprintEffects:
    """Named effects for _worktree_execplan_input_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _execplan_input_paths: Callable[..., object]


def _worktree_execplan_input_fingerprint(effects: WorktreeExecplanInputFingerprintEffects, worktree: Path, plan: sqlite3.Row) -> str:
    entries = []
    paths = effects._execplan_input_paths(plan)
    requirement_documents = (
        requirement_sources.worktree_requirement_documents(worktree)
        if any(str(path).startswith("docs/requirements/") and str(path).count("/") == 2
               for path in paths)
        else {}
    )
    for relative_path in paths:
        if str(relative_path).startswith("docs/requirements/") and str(relative_path).count("/") == 2:
            resolved = requirement_sources.resolve_requirement_source(
                str(relative_path), requirement_documents, allow_archive=True,
            )
            if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError(
                    f"ExecPlan source가 모호합니다: {relative_path} ({', '.join(resolved.candidates)})"
                )
            entries.append((relative_path, resolved.contents))
            continue
        path = worktree / relative_path
        try:
            regular = stat.S_ISREG(path.stat().st_mode)
        except FileNotFoundError:
            regular = False
        contents = path.read_text(encoding="utf-8") if regular else None
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanInputFingerprintEffects:
    """Named effects for _revision_execplan_input_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _execplan_input_paths: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]


def _revision_execplan_input_fingerprint(
    effects: RevisionExecplanInputFingerprintEffects, repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    entries = []
    paths = effects._execplan_input_paths(plan)
    requirement_documents = (
        revision_requirement_documents(effects.git_bytes, repo_path, revision)
        if any(str(path).startswith("docs/requirements/") and str(path).count("/") == 2
               for path in paths)
        else {}
    )
    for relative_path in paths:
        if str(relative_path).startswith("docs/requirements/") and str(relative_path).count("/") == 2:
            resolved = requirement_sources.resolve_requirement_source(
                str(relative_path), requirement_documents, allow_archive=True,
            )
            if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError(
                    f"ExecPlan source가 모호합니다: {relative_path} ({', '.join(resolved.candidates)})"
                )
            entries.append((relative_path, resolved.contents))
            continue
        contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanSourceFingerprintEffects:
    """Named effects for _revision_execplan_source_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]


def _revision_execplan_source_fingerprint(
    effects: RevisionExecplanSourceFingerprintEffects, repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    """Fingerprint only current source documents; the plan body is classified separately."""

    entries = []
    source_paths = json.loads(str(plan["sources"]))
    requirement_documents = (
        revision_requirement_documents(effects.git_bytes, repo_path, revision)
        if any(str(path).startswith("docs/requirements/") and str(path).count("/") == 2
               for path in source_paths)
        else {}
    )
    for relative_path in source_paths:
        if relative_path == "manager:verify-inputs":
            from loop_manager_internal.catalog.manager_sources import source_content
            entries.append((relative_path, source_content(relative_path)))
            continue
        if str(relative_path).startswith("docs/requirements/") and str(relative_path).count("/") == 2:
            resolved = requirement_sources.resolve_requirement_source(
                str(relative_path), requirement_documents, allow_archive=True,
            )
            if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                raise RuntimeError(
                    f"ExecPlan source가 모호합니다: {relative_path} ({', '.join(resolved.candidates)})"
                )
            contents = resolved.contents
        else:
            contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        entries.append((str(relative_path), contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanBodyHashEffects:
    """Named effects for _revision_execplan_body_hash; no entry-point/module access."""
    git: Callable[..., object]


def _revision_execplan_body_hash(
    effects: RevisionExecplanBodyHashEffects, repo_path: Path,
    revision: str,
    path: str,
) -> str:
    contents = revision_document_or_none(effects.git, repo_path, revision, path)
    if contents is None:
        raise RuntimeError(f"최신 ExecPlan 본문을 읽을 수 없습니다: {path}")
    return execution_revision.ExecPlanBodyHash(contents)


@dataclass(frozen=True)
class RevisionChangedPathsEffects:
    """Named effects for _revision_changed_paths; no entry-point/module access."""
    git: Callable[..., object]


def _revision_changed_paths(
    effects: RevisionChangedPathsEffects, repo_path: Path,
    previous_revision: str,
    current_revision: str,
) -> tuple[str, ...]:
    if not previous_revision or previous_revision == current_revision:
        return ()
    changed = effects.git(
        repo_path,
        "diff",
        "--name-only",
        "--find-renames",
        previous_revision,
        current_revision,
        "--",
        check=False,
    )
    if changed.returncode:
        raise RuntimeError("실행 base revision 차이를 확인할 수 없습니다.")
    return tuple(
        sorted(
            {
                line.strip()
                for line in changed.stdout.splitlines()
                if line.strip()
            }
        )
    )


def unmet_execplan_dependencies(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    """Return every direct or indirect dependency not confirmed MERGED."""

    rows = connection.execute(
        "SELECT plan_id, depends_on, status FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    graph = {
        str(row["plan_id"]): (
            tuple(str(item) for item in json.loads(str(row["depends_on"]))),
            str(row["status"]),
        )
        for row in rows
    }
    if plan_id not in graph:
        raise RuntimeError(f"dependency를 확인할 ExecPlan이 없습니다: {plan_id}")
    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    held = catalog_held_plan_ids(connection, repo)
    unmet: set[str] = {plan_id} if plan_id in held else set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        visited.add(current)
        dependencies, _status = graph[current]
        for dependency in dependencies:
            stored = graph.get(dependency)
            if dependency in held or stored is None or stored[1] != "MERGED":
                unmet.add(dependency)
            if stored is not None:
                visit(dependency)

    visit(plan_id)
    return tuple(sorted(unmet))


@dataclass(frozen=True)
class RevisionReviewIssueFingerprintEffects:
    """Named effects for _revision_review_issue_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _review_issue_paths: Callable[..., object]
    git: Callable[..., object]


def _revision_review_issue_fingerprint(
    effects: RevisionReviewIssueFingerprintEffects, repo_path: Path,
    revision: str,
    review_result: object,
) -> str | None:
    paths = effects._review_issue_paths(review_result)
    if not paths:
        return None
    entries = []
    for relative_path in paths:
        contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


def _blocked_validation_base_revision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> str:
    project = connection.execute(
        "SELECT active_sha FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    return str(
        (project["active_sha"] if project else None)
        or plan["active_sha"]
        or plan["current_revision_sha"]
        or plan["plan_sha"]
        or ""
    )


@dataclass(frozen=True)
class BlockedValidationInputFingerprintsEffects:
    """Named effects for _blocked_validation_input_fingerprints; no entry-point/module access."""
    _blocked_validation_base_revision: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    git: Callable[..., object]


def _blocked_validation_input_fingerprints(
    effects: BlockedValidationInputFingerprintsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
) -> tuple[str, str]:
    """Capture comparable base and worktree inputs at the blocking boundary."""

    worktree_fingerprint = effects._worktree_execplan_input_fingerprint(worktree, plan)
    revision = effects._blocked_validation_base_revision(connection, repo, plan)
    if not revision:
        return worktree_fingerprint, worktree_fingerprint
    resolved = effects.git(
        repo_path,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    if resolved.returncode:
        return worktree_fingerprint, worktree_fingerprint
    return (
        effects._revision_execplan_input_fingerprint(repo_path, revision, plan),
        worktree_fingerprint,
    )


def restore_worker_frontmatter(worktree, relative_path, plan, *, repo, revision, connection, git_bytes):
    """Worker 종료 뒤 선택된 공식 선언만 복원하고 본문 bytes를 보존한다."""
    if relative_path != plan['path'] or not revision:
        raise ExecPlanMetadataError('frontmatter 복원의 공식 경로·revision이 없습니다.')
    root = worktree.resolve()
    target = root / relative_path
    if target.resolve() != target or not stat.S_ISREG(target.lstat().st_mode):
        raise ExecPlanMetadataError('frontmatter 복원 대상이 실제 작업 파일이 아닙니다.')
    candidate = target.read_bytes()
    def split(raw):
        lines = raw.splitlines(keepends=True)
        if not lines or lines[0].rstrip(b'\r\n') != b'---':
            raise ExecPlanMetadataError('frontmatter 복원에 필요한 본문 경계가 없습니다.')
        for index, line in enumerate(lines[1:], 1):
            if line.rstrip(b'\r\n') == b'---':
                return b''.join(lines[:index + 1]), b''.join(lines[index + 1:])
        raise ExecPlanMetadataError('frontmatter 복원에 필요한 종료 구분자가 없습니다.')
    current, body = split(candidate)
    if connection is None:
        raise RuntimeError('frontmatter 복원 감사 DB가 없습니다.')
    entry = connection.execute('SELECT agent_input_plan FROM exec_plans WHERE repo = ? AND plan_id = ?',
                               (repo, str(plan['plan_id']))).fetchone()
    if entry is None or not entry['agent_input_plan']:
        return False  # 시작 증거가 없는 과거 후보는 기존 metadata 검증으로 판단한다.
    started, _ = split(str(entry['agent_input_plan']).encode('utf-8'))
    if current.replace(b'\r\n', b'\n') == started.replace(b'\r\n', b'\n'):
        return False
    original = git_bytes(worktree, 'show', f'{revision}:{relative_path}')
    official = execplan_catalog.parse_execplan_text(relative_path, original.decode('utf-8'), validate_sources=False)
    error = _execplan_metadata_identity_error(
        ExecplanMetadataIdentityErrorEffects(_current_execplan_metadata=_current_execplan_metadata),
        worktree, official, plan, 'frontmatter 복원 원본')
    if error is not None or official.get('draft'):
        raise ExecPlanMetadataError(error or 'draft 선언은 자동 복원하지 않습니다.')
    desired, _ = split(original)
    if started.replace(b'\r\n', b'\n') != desired.replace(b'\r\n', b'\n'):
        raise RuntimeError('Worker 시작 이전의 frontmatter 변경은 자동 복원하지 않습니다.')
    from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
    checkpoints = ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, str(plan['plan_id']))
    if checkpoint.operation is not None:
        raise RuntimeError('진행 중 Manager Git operation에서는 frontmatter를 별도 복원하지 않습니다.')
    record = {'path': relative_path, 'revision': revision,
              'before': base64.b64encode(current).decode(), 'after': base64.b64encode(desired).decode(),
              'body_sha256': hashlib.sha256(body).hexdigest()}
    snapshot = checkpoint.snapshot or {}
    checkpoints.record(repo, str(plan['plan_id']), expected_generation=checkpoint.generation,
                       operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                       target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                       snapshot={**snapshot, 'worker_frontmatter_restores':
                                 [*snapshot.get('worker_frontmatter_restores', []), record]})
    connection.commit()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.frontmatter-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(desired + body)
            stream.flush()
            os.fsync(stream.fileno())
        mode = stat.S_IMODE(target.stat().st_mode)
        os.chmod(temporary, mode)
        if target.read_bytes() != candidate:
            raise RuntimeError('frontmatter 복원 전 파일이 다시 변경됐습니다.')
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


@dataclass(frozen=True)
class ExecplanMetadataIdentityErrorEffects:
    """Named effects for _execplan_metadata_identity_error; no entry-point/module access."""
    _current_execplan_metadata: Callable[..., object]


def _execplan_metadata_identity_error(
    effects: ExecplanMetadataIdentityErrorEffects, repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    del repo_path
    if current["id"] != plan["plan_id"]:
        return f"{subject}의 ID가 reconciled catalog identity와 다릅니다."
    reconciled = effects._current_execplan_metadata(plan, current)
    metadata_fields = (
        "title",
        "sources",
        "depends_on",
        "complexity",
        "risk",
        "priority",
    )
    mismatched = [field for field in metadata_fields if current[field] != reconciled[field]]
    if mismatched:
        return f"{subject} metadata가 reconciled catalog와 다릅니다. 불일치 필드: {', '.join(mismatched)}"
    return None


def _current_execplan_metadata(
    plan: sqlite3.Row,
    fallback: Mapping[str, object],
) -> dict[str, object]:
    """Expose the reconciled declaration cache without publication-audit fields."""

    complexity = plan["current_complexity"] or fallback.get("complexity")
    risk = plan["current_risk"] or fallback.get("risk")
    return {
        "id": str(plan["plan_id"]),
        "title": str(plan["title"]),
        "sources": list(json.loads(str(plan["sources"]))),
        "depends_on": list(json.loads(str(plan["depends_on"]))),
        "complexity": str(complexity),
        "risk": str(risk),
        "priority": str(plan["priority"]),
    }


@dataclass(frozen=True)
class BlockedExecplanMetadataIdentityErrorEffects:
    """Named effects for _blocked_execplan_metadata_identity_error; no entry-point/module access."""
    _execplan_metadata_identity_error: Callable[..., object]


def _blocked_execplan_metadata_identity_error(
    effects: BlockedExecplanMetadataIdentityErrorEffects, repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
) -> str | None:
    return effects._execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "BLOCKED ExecPlan",
    )


@dataclass(frozen=True)
class ValidateActiveExecplanMetadataEffects:
    """Named effects for validate_active_execplan_metadata; no entry-point/module access."""
    _current_execplan_metadata: Callable[..., object]
    _execplan_metadata_identity_error: Callable[..., object]
    validate_execplan: Callable[..., object]


def validate_active_execplan_metadata(
    effects: ValidateActiveExecplanMetadataEffects, repo_path: Path,
    worktree: Path,
    relative_path: str,
    plan: sqlite3.Row,
    *,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
    rebase_target_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        current = effects.validate_execplan(worktree, relative_path)
        if current.get("draft"):
            raise ExecPlanMetadataError("사용자 판단 대기 draft ExecPlan은 실행할 수 없습니다.")
    except (ExternalObservationError, GitLockError):
        raise
    except OSError:
        raise
    except RuntimeError as error:
        raise ExecPlanMetadataError(str(error)) from error
    if rebase_target_metadata is not None:
        # Only the checkpoint-bound rebase service supplies this boundary.
        # Compare whole declarations, never a mixture of old and new fields.
        # Normal execution and final publication still compare with catalog.
        if (rebase_definition is None or rebase_definition.path != relative_path
                or relative_path != str(plan['path'])
                or current['id'] != plan['plan_id']
                or rebase_target_metadata['id'] != plan['plan_id']
                or rebase_target_metadata.get('draft')):
            raise ExecPlanMetadataError('소유된 rebase ExecPlan identity가 다릅니다.')
        fields = ('title', 'sources', 'depends_on', 'complexity', 'risk', 'priority')
        applied = {field: getattr(rebase_definition, field) for field in fields}
        for field in ('sources', 'depends_on'):
            applied[field] = list(applied[field])
        if not any(all(current[field] == declaration[field] for field in fields)
                   for declaration in (applied, rebase_target_metadata)):
            raise ExecPlanMetadataError(
                '소유된 rebase ExecPlan metadata가 적용된 선언 또는 target base와 다릅니다.')
        return current
    comparison_plan = plan
    if rebase_definition is not None:
        # A paused rebase still contains its applied declaration. Scheduling,
        # profile and dependency fields may lag during conflict resolution;
        # all other identity fields and all normal validation remain strict.
        if rebase_definition.path != relative_path:
            raise ExecPlanMetadataError("rebase 기준 ExecPlan 경로가 현재 작업 경로와 다릅니다.")
        comparison_plan = dict(plan)
        for field, column in (
            ('priority', 'priority'),
            ('complexity', 'current_complexity'),
            ('risk', 'current_risk'),
        ):
            applied = getattr(rebase_definition, field)
            if current[field] == applied:
                comparison_plan[column] = applied
        if tuple(current['depends_on']) == rebase_definition.depends_on:
            comparison_plan['depends_on'] = json.dumps(rebase_definition.depends_on)
    error = effects._execplan_metadata_identity_error(
        repo_path,
        current,
        comparison_plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise ExecPlanMetadataError(error)
    if relative_path != str(plan["path"]):
        raise ExecPlanMetadataError("활성 ExecPlan 경로가 reconciled catalog와 다릅니다.")
    return effects._current_execplan_metadata(plan, current)
