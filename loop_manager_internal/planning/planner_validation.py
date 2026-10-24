"""planning planner validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, replace
from loop_manager_internal.catalog import execplan_catalog, project_catalog_refresh
from loop_manager_internal.catalog import execplan_drafts
from loop_manager_internal.application.contracts import PlanIdAllocation, ValidatedPlanningResult
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
from loop_manager_internal.storage import maintenance_state, runtime_state
from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
from pathlib import Path
import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
from uuid import uuid4


class MutableReadyPRConflict(RuntimeError):
    """A live PR appeared after its plan entered a mutable snapshot."""


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


def _planning_id_inventory(
    worktree: Path,
    new_plan_paths: set[str],
    snapshot: MutableReadySnapshot | None,
    connection: sqlite3.Connection | None,
    repo: str | None,
    held_ids: frozenset[str],
) -> tuple[set[str], set[str]]:
    """Collect occupied IDs and actual plan identities before normalization."""
    existing_plan_ids = (
        {plan.plan_id for plan in snapshot.plans} if snapshot is not None else set()
    )
    reserved = existing_plan_ids | set(held_ids)
    for relative_directory in ("docs/plans", "docs/plans/archive"):
        directory = worktree / relative_directory
        try:
            paths = sorted(directory.glob("*.md"))
        except OSError as error:
            raise ExternalObservationError(
                f"ExecPlan directory를 읽을 수 없습니다: {relative_directory}"
            ) from error
        for path in paths:
            relative_path = path.relative_to(worktree).as_posix()
            if relative_path in new_plan_paths:
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ExternalObservationError(
                    f"ExecPlan ID를 읽을 수 없습니다: {relative_path}"
                ) from error
            except UnicodeError:
                # The full catalog validation below reports invalid encoding.
                continue
            try:
                plan_id = execplan_catalog.parse_frontmatter(text).get("id")
            except RuntimeError:
                # The full catalog validation below reports malformed documents.
                continue
            if isinstance(plan_id, str) and re.fullmatch(r"G-\d{3,}", plan_id):
                reserved.add(plan_id)
                existing_plan_ids.add(plan_id)

    if connection is not None and repo is not None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in ("exec_plans", "execplan_cancellations", "manual_work_members"):
            if table not in tables:
                continue
            for row in connection.execute(
                f"SELECT plan_id FROM {table} WHERE repo = ?", (repo,)
            ):
                plan_id = str(row[0])
                reserved.add(plan_id)
                if table == "exec_plans":
                    existing_plan_ids.add(plan_id)
    return reserved, existing_plan_ids


def _plan_id_normalization_journal_path(worktree: Path) -> Path:
    identity = hashlib.sha256(str(worktree.resolve()).encode("utf-8")).hexdigest()
    return worktree.parent / f".loop-manager-plan-id-{identity}.json"


def _sync_normalization_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_plan_normalization_temp(path: Path, contents: bytes) -> None:
    with path.open("xb") as stream:
        written = stream.write(contents)
        if written != len(contents):
            raise OSError("short plan normalization write")
        stream.flush()
        os.fsync(stream.fileno())


def _persist_plan_id_normalization_journal(
    worktree: Path, journal: dict[str, object],
) -> Path:
    path = _plan_id_normalization_journal_path(worktree)
    payload = json.dumps(journal, ensure_ascii=True, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("short plan normalization journal write")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_normalization_directory(path.parent)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ExternalObservationError(
            f"새 ExecPlan 정규화 복구 정보를 보존할 수 없습니다: {error}"
        ) from error
    return path


def _read_plan_id_normalization_journal(
    worktree: Path, path: Path,
) -> tuple[str, list[dict[str, object]]]:
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PlannerValidationError(
            "새 ExecPlan 정규화 복구 정보를 읽을 수 없습니다. 작업 공간을 보존했습니다."
        ) from error
    if (
        not isinstance(journal, dict)
        or journal.get("version") != 1
        or journal.get("worktree") != str(worktree.resolve())
        or not isinstance(journal.get("transaction"), str)
        or re.fullmatch(r"[0-9a-f]{32}", journal["transaction"]) is None
        or not isinstance(journal.get("plans"), list)
        or not journal["plans"]
        or not isinstance(journal.get("related", []), list)
    ):
        raise PlannerValidationError(
            "새 ExecPlan 정규화 복구 정보의 identity가 유효하지 않습니다. 작업 공간을 보존했습니다."
        )
    transaction = journal["transaction"]
    plans: list[dict[str, object]] = []
    sources: set[str] = set()
    targets: set[str] = set()
    source_ids: set[str] = set()
    target_ids: set[str] = set()
    id_mapping: dict[str, str] = {}
    for item in journal["plans"] + journal.get("related", []):
        if not isinstance(item, dict) or set(item) != {
            "kind", "source_path", "target_path", "original", "original_hash",
            "output_hash", "mode", "temporary_path",
        } or item.get("kind") not in {"new", "related"}:
            raise PlannerValidationError(
                "새 ExecPlan 정규화 복구 정보에 유효하지 않은 항목이 있습니다. 작업 공간을 보존했습니다."
            )
        source = item["source_path"]
        target = item["target_path"]
        temporary = item["temporary_path"]
        source_match = (
            re.fullmatch(r"docs/plans/\[(G-\d{3,})\] ([^/\\]+)\.md", source)
            if isinstance(source, str) else None
        )
        target_match = (
            re.fullmatch(r"docs/plans/\[(G-\d{3,})\] ([^/\\]+)\.md", target)
            if isinstance(target, str) else None
        )
        if (
            source_match is None
            or target_match is None
            or source_match.group(2) != target_match.group(2)
            or not isinstance(temporary, str)
            or re.fullmatch(
                rf"docs/plans/\.plan-id-normalize-{transaction}-\d+\.tmp",
                temporary,
            ) is None
            or source in sources
            or target in targets
            or (item["kind"] == "new" and (
                source_match.group(1) in source_ids
                or target_match.group(1) in target_ids
            ))
            or (item["kind"] == "related" and (
                source != target or source_match.group(1) != target_match.group(1)
            ))
            or type(item["mode"]) is not int
            or not 0 <= item["mode"] <= 0o7777
        ):
            raise PlannerValidationError(
                "새 ExecPlan 정규화 복구 경로가 유효하지 않습니다. 작업 공간을 보존했습니다."
            )
        try:
            original = base64.b64decode(item["original"], validate=True)
        except (TypeError, ValueError) as error:
            raise PlannerValidationError(
                "새 ExecPlan 정규화 원본 복구 데이터가 유효하지 않습니다. 작업 공간을 보존했습니다."
            ) from error
        if (
            not isinstance(item["original_hash"], str)
            or hashlib.sha256(original).hexdigest() != item["original_hash"]
            or not isinstance(item["output_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", item["output_hash"]) is None
        ):
            raise PlannerValidationError(
                "새 ExecPlan 정규화 복구 hash가 유효하지 않습니다. 작업 공간을 보존했습니다."
            )
        try:
            original_text = original.decode("utf-8")
            metadata = execplan_catalog.parse_frontmatter(original_text)
            if (
                metadata.get("id") != source_match.group(1)
                or metadata.get("title") != source_match.group(2)
            ):
                raise ValueError("source identity mismatch")
        except (UnicodeError, RuntimeError, ValueError) as error:
            raise PlannerValidationError(
                "새 ExecPlan 정규화 원본 identity가 복구 정보와 다릅니다. 작업 공간을 보존했습니다."
            ) from error
        validated = dict(item)
        validated["original_bytes"] = original
        validated["source_id"] = source_match.group(1)
        validated["target_id"] = target_match.group(1)
        plans.append(validated)
        sources.add(source)
        targets.add(target)
        if item["kind"] == "new":
            source_ids.add(source_match.group(1))
            target_ids.add(target_match.group(1))
            id_mapping[source_match.group(1)] = target_match.group(1)
    for item in plans:
        try:
            rewritten = _rewrite_plan_identity(
                bytes(item["original_bytes"]).decode("utf-8"),
                str(item["target_id"]),
                id_mapping,
                rewrite_id=str(item["kind"]) == "new",
            ).encode("utf-8")
        except (UnicodeError, PlannerValidationError) as error:
            raise PlannerValidationError(
                "새 ExecPlan 정규화 출력이 복구 정보와 일치하지 않습니다. 작업 공간을 보존했습니다."
            ) from error
        if hashlib.sha256(rewritten).hexdigest() != item["output_hash"]:
            raise PlannerValidationError(
                "새 ExecPlan 정규화 출력 hash가 복구 정보와 다릅니다. 작업 공간을 보존했습니다."
            )
    return transaction, plans


def _recover_interrupted_plan_id_normalization(worktree: Path) -> set[str]:
    """Restore originals and return journal-owned paths removed by recovery."""
    journal_path = _plan_id_normalization_journal_path(worktree)
    try:
        journal_info = journal_path.lstat()
    except FileNotFoundError:
        return set()
    except OSError as error:
        raise ExternalObservationError(
            f"새 ExecPlan 정규화 복구 정보를 확인할 수 없습니다: {error}"
        ) from error
    if not stat.S_ISREG(journal_info.st_mode):
        raise PlannerValidationError(
            "새 ExecPlan 정규화 복구 정보가 일반 파일이 아닙니다. 작업 공간을 보존했습니다."
        )
    _transaction, plans = _read_plan_id_normalization_journal(worktree, journal_path)
    source_paths = {str(item["source_path"]) for item in plans}
    outputs_by_path: dict[str, set[str]] = {}
    for item in plans:
        outputs_by_path.setdefault(str(item["target_path"]), set()).add(
            str(item["output_hash"])
        )

    for item in plans:
        temporary = worktree / str(item["temporary_path"])
        try:
            info = temporary.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ExternalObservationError(
                f"새 ExecPlan 정규화 임시 파일을 확인할 수 없습니다: {error}"
            ) from error
        if stat.S_ISDIR(info.st_mode):
            raise PlannerValidationError(
                "새 ExecPlan 정규화 임시 경로가 디렉터리입니다. 작업 공간을 보존했습니다."
            )
        temporary.unlink()

    unexpected_targets: list[str] = []
    for target in set(outputs_by_path) - source_paths:
        path = worktree / target
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ExternalObservationError(
                f"새 ExecPlan 정규화 대상을 확인할 수 없습니다: {target} ({error})"
            ) from error
        if not stat.S_ISREG(info.st_mode) or hashlib.sha256(path.read_bytes()).hexdigest() not in outputs_by_path[target]:
            unexpected_targets.append(target)
            continue
        path.unlink()
        _sync_normalization_directory(path.parent)

    for item in plans:
        source = worktree / str(item["source_path"])
        temporary = worktree / str(item["temporary_path"])
        original = bytes(item["original_bytes"])
        allowed_hashes = {str(item["original_hash"])} | outputs_by_path.get(
            str(item["source_path"]), set()
        )
        try:
            info = source.lstat()
        except FileNotFoundError:
            info = None
        except OSError as error:
            raise ExternalObservationError(
                f"새 ExecPlan 정규화 원본을 확인할 수 없습니다: {item['source_path']} ({error})"
            ) from error
        if info is not None:
            if not stat.S_ISREG(info.st_mode) or hashlib.sha256(source.read_bytes()).hexdigest() not in allowed_hashes:
                raise PlannerValidationError(
                    f"새 ExecPlan 정규화 원본이 예상과 달라 복구를 보류했습니다: {item['source_path']}"
                )
        _write_plan_normalization_temp(temporary, original)
        os.chmod(temporary, int(item["mode"]))
        os.replace(temporary, source)
        _sync_normalization_directory(source.parent)

    if unexpected_targets:
        raise PlannerValidationError(
            "새 ExecPlan 정규화 대상을 안전하게 복구할 수 없습니다: "
            + ", ".join(sorted(unexpected_targets))
        )
    journal_path.unlink()
    _sync_normalization_directory(journal_path.parent)
    return ({str(item["temporary_path"]) for item in plans}
            | (set(outputs_by_path) - source_paths))


def _rewrite_plan_identity(
    text: str, plan_id: str | None, dependency_ids: dict[str, str], *,
    rewrite_id: bool = True,
) -> str:
    """Change only frontmatter identity and structural dependency list entries."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise PlannerValidationError("ExecPlan frontmatter를 정규화할 수 없습니다.")
    in_dependencies = False
    replaced_id = False
    for index in range(1, len(lines)):
        line = lines[index]
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if content == "---":
            break
        if in_dependencies and content.startswith("  - "):
            dependency = content[4:].strip()
            if dependency in dependency_ids:
                lines[index] = f"  - {dependency_ids[dependency]}{ending}"
            continue
        in_dependencies = False
        if ":" not in content:
            continue
        key, raw_value = content.split(":", 1)
        if key.strip() == "id" and rewrite_id:
            match = re.fullmatch(r"(\s*)G-\d{3,}(\s*)", raw_value)
            if match is None:
                raise PlannerValidationError("ExecPlan ID 항목을 정규화할 수 없습니다.")
            lines[index] = f"{key}:{match.group(1)}{plan_id}{match.group(2)}{ending}"
            replaced_id = True
            continue
        if key.strip() == "depends_on" and not raw_value.strip():
            in_dependencies = True
    if rewrite_id and not replaced_id:
        raise PlannerValidationError("ExecPlan ID 항목을 정규화할 수 없습니다.")
    return "".join(lines)


def _execplan_body(text: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            return "".join(lines[index + 1:])
    raise PlannerValidationError("ExecPlan frontmatter 종료 구분자를 찾을 수 없습니다.")


def _normalize_new_plan_batch(
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    reserved_ids: set[str],
    existing_plan_ids: set[str],
    parse_execplan: Callable[..., object],
    related_plans: tuple[tuple[str, dict[str, object]], ...] = (),
) -> tuple[list[tuple[str, dict[str, object]]], tuple[PlanIdAllocation, ...]]:
    """Assign new plans a fresh consecutive range and rewrite only certain edges."""
    _recover_interrupted_plan_id_normalization(worktree)
    if not plans:
        return [], ()
    claimed = [str(plan["id"]) for _path, plan in plans]
    if len(claimed) != len(set(claimed)):
        raise PlannerValidationError(
            "새 ExecPlan batch에 같은 ID가 여러 목표로 중복돼 assignment가 모호합니다."
        )

    ordered = sorted(plans, key=lambda item: (int(str(item[1]["id"])[2:]), item[0]))
    first_number = max((int(plan_id[2:]) for plan_id in reserved_ids), default=0) + 1
    id_mapping = {
        str(plan["id"]): f"G-{first_number + index:03d}"
        for index, (_path, plan) in enumerate(ordered)
    }
    collisions = set(id_mapping) & reserved_ids
    changed_ids = {
        source_id for source_id, target_id in id_mapping.items()
        if source_id != target_id
    }
    batch_ids = set(claimed)
    original_files: dict[str, tuple[bytes, int, str]] = {}
    related_files: dict[str, tuple[bytes, int, str]] = {}
    related_texts: dict[str, str] = {}
    for path, plan in ordered:
        dependencies = {str(item) for item in plan["depends_on"]}
        ambiguous = sorted(dependencies & collisions)
        if ambiguous:
            raise PlannerValidationError(
                "새 batch dependency가 기존 ID와 충돌 ID를 함께 가리켜 모호합니다: "
                f"{path} -> {', '.join(ambiguous)}"
            )
        missing = sorted(
            dependencies - batch_ids - existing_plan_ids
        )
        if missing:
            raise PlannerValidationError(
                "새 ExecPlan dependency 대상이 없습니다. "
                "존재하지 않는 depends_on ID: "
                f"{path} -> {', '.join(missing)}"
            )
        try:
            source = worktree / path
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise PlannerValidationError(
                    f"새 ExecPlan은 symlink가 아닌 일반 파일이어야 합니다: {path}"
                )
            original_bytes = source.read_bytes()
            original_text = original_bytes.decode("utf-8")
            original_files[path] = (
                original_bytes, stat.S_IMODE(info.st_mode), original_text,
            )
            body = _execplan_body(original_text)
        except FileNotFoundError as error:
            raise ExternalObservationError(
                f"새 ExecPlan ID 참조를 읽을 수 없습니다: {path}"
            ) from error
        except OSError as error:
            raise ExternalObservationError(
                f"새 ExecPlan ID 참조를 읽을 수 없습니다: {path}"
            ) from error
        except UnicodeError as error:
            raise PlannerValidationError(
                f"새 ExecPlan ID 참조가 UTF-8이 아닙니다: {path}"
            ) from error
        references = sorted(
            plan_id for plan_id in changed_ids
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(plan_id)}(?!\d)", body)
        )
        if references:
            raise PlannerValidationError(
                "새 ID를 바꾸면 ExecPlan 본문 참조 대상을 확정할 수 없습니다: "
                f"{path} -> {', '.join(references)}"
            )

    for path, plan in related_plans:
        dependencies = {str(item) for item in plan["depends_on"]}
        ambiguous = sorted(dependencies & collisions)
        if ambiguous:
            raise PlannerValidationError(
                "수정 계획 dependency가 기존 ID와 충돌 ID를 함께 가리켜 모호합니다: "
                f"{path} -> {', '.join(ambiguous)}"
            )
        missing = sorted(dependencies - batch_ids - existing_plan_ids)
        if missing:
            raise PlannerValidationError(
                "수정 ExecPlan dependency 대상이 없습니다. "
                "존재하지 않는 depends_on ID: "
                f"{path} -> {', '.join(missing)}"
            )
        try:
            source = worktree / path
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise PlannerValidationError(
                    f"기존 ExecPlan은 symlink가 아닌 일반 파일이어야 합니다: {path}"
                )
            original_bytes = source.read_bytes()
            original_text = original_bytes.decode("utf-8")
            body = _execplan_body(original_text)
            related_files[path] = (
                original_bytes, stat.S_IMODE(info.st_mode), original_text,
            )
        except FileNotFoundError as error:
            raise ExternalObservationError(
                f"수정 ExecPlan ID 참조를 읽을 수 없습니다: {path}"
            ) from error
        except OSError as error:
            raise ExternalObservationError(
                f"수정 ExecPlan ID 참조를 읽을 수 없습니다: {path}"
            ) from error
        except UnicodeError as error:
            raise PlannerValidationError(
                f"수정 ExecPlan ID 참조가 UTF-8이 아닙니다: {path}"
            ) from error
        references = sorted(
            plan_id for plan_id in changed_ids
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(plan_id)}(?!\d)", body)
        )
        if references:
            raise PlannerValidationError(
                "새 ID를 바꾸면 수정 ExecPlan 본문 참조 대상을 확정할 수 없습니다: "
                f"{path} -> {', '.join(references)}"
            )
        updated_text = _rewrite_plan_identity(
            original_text, None, id_mapping, rewrite_id=False,
        )
        if updated_text != original_text:
            related_texts[path] = updated_text

    allocations: list[PlanIdAllocation] = []
    normalized: list[tuple[str, dict[str, object]]] = []
    source_paths = {path for path, _plan in ordered}
    moves: dict[str, str] = {}
    for source_path, plan in ordered:
        old_id = str(plan["id"])
        new_id = id_mapping[old_id]
        target_path = f"docs/plans/[{new_id}] {plan['title']}.md"
        destination = worktree / target_path
        if (target_path != source_path and os.path.lexists(destination)
                and target_path not in source_paths):
            raise PlannerValidationError(
                f"새 ExecPlan 대상 경로가 이미 있어 보존했습니다: {target_path}"
            )
        if target_path != source_path:
            moves[source_path] = target_path
    if any(
        target in source_paths and target not in moves
        for target in moves.values()
    ):
        raise PlannerValidationError(
            "새 ExecPlan 대상 경로가 batch의 기존 계획 파일과 충돌합니다."
        )
    transaction = uuid4().hex
    journal_plans: list[dict[str, object]] = []
    journal_related: list[dict[str, object]] = []
    updated_texts: dict[str, str] = {}
    temporary_paths: dict[str, Path] = {}
    for index, (source_path, plan) in enumerate(ordered):
        old_id = str(plan["id"])
        new_id = id_mapping[old_id]
        target_path = f"docs/plans/[{new_id}] {plan['title']}.md"
        original_bytes, mode, original_text = original_files[source_path]
        try:
            updated_text = _rewrite_plan_identity(original_text, new_id, id_mapping)
        except UnicodeError as error:
            raise PlannerValidationError(
                f"새 ExecPlan UTF-8 내용을 정규화할 수 없습니다: {source_path}"
            ) from error
        updated_bytes = updated_text.encode("utf-8")
        updated_texts[target_path] = updated_text
        temporary_relative = f"docs/plans/.plan-id-normalize-{transaction}-{index}.tmp"
        temporary_paths[target_path] = worktree / temporary_relative
        journal_plans.append({
            "kind": "new",
            "source_path": source_path,
            "target_path": target_path,
            "original": base64.b64encode(original_bytes).decode("ascii"),
            "original_hash": hashlib.sha256(original_bytes).hexdigest(),
            "output_hash": hashlib.sha256(updated_bytes).hexdigest(),
            "mode": mode,
            "temporary_path": temporary_relative,
        })

    for index, path in enumerate(sorted(related_texts), start=len(journal_plans)):
        original_bytes, mode, original_text = related_files[path]
        updated_bytes = related_texts[path].encode("utf-8")
        temporary_relative = f"docs/plans/.plan-id-normalize-{transaction}-{index}.tmp"
        temporary_paths[path] = worktree / temporary_relative
        updated_texts[path] = related_texts[path]
        journal_related.append({
            "kind": "related",
            "source_path": path,
            "target_path": path,
            "original": base64.b64encode(original_bytes).decode("ascii"),
            "original_hash": hashlib.sha256(original_bytes).hexdigest(),
            "output_hash": hashlib.sha256(updated_bytes).hexdigest(),
            "mode": mode,
            "temporary_path": temporary_relative,
        })

    changed = any(
        item["source_path"] != item["target_path"]
        or hashlib.sha256(
            (original_files | related_files)[str(item["source_path"])][0]
        ).hexdigest()
            != str(item["output_hash"])
        for item in journal_plans + journal_related
    )
    journal_path = None
    if changed:
        journal_path = _persist_plan_id_normalization_journal(
            worktree,
            {"version": 1, "worktree": str(worktree.resolve()),
             "transaction": transaction, "plans": journal_plans,
             "related": journal_related},
        )
    try:
        if changed:
            all_journal_items = journal_plans + journal_related
            all_original_files = original_files | related_files
            for item in all_journal_items:
                target_path = str(item["target_path"])
                original_bytes, mode, _text = all_original_files[str(item["source_path"])]
                if (
                    str(item["source_path"]) == target_path
                    and hashlib.sha256(original_bytes).hexdigest() == str(item["output_hash"])
                ):
                    continue
                temporary = temporary_paths[target_path]
                _write_plan_normalization_temp(
                    temporary, updated_texts[target_path].encode("utf-8"),
                )
                os.chmod(temporary, mode)

            for item in all_journal_items:
                source = worktree / str(item["source_path"])
                try:
                    info = source.lstat()
                except OSError as error:
                    raise ExternalObservationError(
                        f"새 ExecPlan 정규화 원본을 다시 확인할 수 없습니다: {item['source_path']}"
                    ) from error
                if not stat.S_ISREG(info.st_mode):
                    raise PlannerValidationError(
                        f"새 ExecPlan 정규화 중 원본 파일 종류가 변경됐습니다: {item['source_path']}"
                    )
                try:
                    current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                except OSError as error:
                    raise ExternalObservationError(
                        f"새 ExecPlan 정규화 원본을 다시 읽을 수 없습니다: {item['source_path']}"
                    ) from error
                if current_hash != item["original_hash"]:
                    raise PlannerValidationError(
                        f"새 ExecPlan 정규화 중 원본이 변경됐습니다: {item['source_path']}"
                    )

            all_source_paths = source_paths | set(related_texts)
            unchanged_paths = {
                str(item["source_path"])
                for item in all_journal_items
                if (
                    item["source_path"] == item["target_path"]
                    and item["original_hash"] == item["output_hash"]
                )
            }
            for source_path in sorted(all_source_paths - unchanged_paths):
                (worktree / source_path).unlink()
                _sync_normalization_directory((worktree / source_path).parent)
            for item in all_journal_items:
                source_path = str(item["source_path"])
                target_path = str(item["target_path"])
                original_bytes = all_original_files[source_path][0]
                if (
                    source_path == target_path
                    and hashlib.sha256(original_bytes).hexdigest() == str(item["output_hash"])
                ):
                    continue
                staged = temporary_paths[target_path]
                destination = worktree / target_path
                if source_path == target_path:
                    os.replace(staged, destination)
                else:
                    # link creates the complete staged file atomically and
                    # fails if another writer claimed the target meanwhile.
                    os.link(staged, destination)
                    staged.unlink()
                _sync_normalization_directory(destination.parent)

        normalized: list[tuple[str, dict[str, object]]] = []
        allocations: list[PlanIdAllocation] = []
        for source_path, plan in ordered:
            old_id = str(plan["id"])
            new_id = id_mapping[old_id]
            target_path = f"docs/plans/[{new_id}] {plan['title']}.md"
            try:
                updated_plan = dict(parse_execplan(
                    worktree,
                    worktree / target_path,
                    require_priority=True,
                    allow_archive_fallback=False,
                ))
            except (OSError, UnicodeError, RuntimeError) as error:
                if isinstance(error, (ExternalObservationError, GitLockError)):
                    raise
                raise PlannerValidationError(
                    f"정규화된 ExecPlan을 다시 읽을 수 없습니다: {target_path} ({error})"
                ) from error
            if (
                str(updated_plan.get("id")) != new_id
                or tuple(str(item) for item in updated_plan.get("depends_on", ()))
                != tuple(id_mapping.get(str(item), str(item)) for item in plan["depends_on"])
            ):
                raise PlannerValidationError(
                    f"정규화된 ExecPlan identity가 배정 결과와 다릅니다: {target_path}"
                )
            normalized.append((target_path, updated_plan))
            allocations.append(PlanIdAllocation(source_path, old_id, target_path, new_id))
        if journal_path is not None:
            journal_path.unlink()
            _sync_normalization_directory(journal_path.parent)
        return normalized, tuple(allocations)
    except BaseException as error:
        if journal_path is not None:
            try:
                _recover_interrupted_plan_id_normalization(worktree)
            except BaseException as recovery_error:
                raise ExternalObservationError(
                    "새 ExecPlan 정규화 실패 후 원본 복구에 실패했습니다. 작업 공간과 복구 정보를 보존했습니다."
                ) from recovery_error
        if isinstance(error, OSError):
            raise ExternalObservationError(
                f"새 ExecPlan batch를 원자적으로 정규화할 수 없습니다: {error}"
            ) from error
        raise


@dataclass(frozen=True)
class ValidatePlannerResultEffects:
    """Named effects for validate_planner_result; no entry-point/module access."""
    _parse_execplan: Callable[..., object]
    _stored_string_list: Callable[..., object]
    _validate_future_idea_separation: Callable[..., object]
    changed_files: Callable[..., object]
    file_hash: Callable[..., object]
    git: Callable[..., object]
    load_execplan_catalog: Callable[..., object]
    mutable_snapshot_token: Callable[..., object]
    _assert_mutable_ready_snapshot_current: Callable[..., object] | None = None


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
    recovered_paths = _recover_interrupted_plan_id_normalization(worktree)
    resolutions = {}
    withdrawals = {}
    if connection is not None and repo is not None and mutable_snapshot is not None:
        from loop_manager_internal.catalog import draft_decisions
        resolutions = draft_decisions.prepare(connection, repo, worktree, mutable_snapshot)
        from loop_manager_internal.catalog import remote_deletions
        withdrawals = remote_deletions.prepare_withdrawals(
            connection, repo, worktree, mutable_snapshot,
            assert_current=effects._assert_mutable_ready_snapshot_current)
    # A resumed batch can still have Manager moves in the index/worktree.
    # Saved dirty paths are hints, not proof that a receipted move exists.
    observed = (set(effects.changed_files(worktree, active_sha))
                | set(effects.changed_files(worktree)))
    if not set(manager_paths) <= observed:
        raise PlannerValidationError("Manager Requirement 이동 영수증이 실제 변경과 다릅니다.")
    planned_files = sorted((observed | (set(dirty_files) - recovered_paths)) - set(manager_paths))
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
        plan_file = worktree / changed_path
        try:
            file_info = plan_file.lstat()
        except FileNotFoundError:
            file_info = None
        except OSError as error:
            raise ExternalObservationError(
                f"Planner ExecPlan 경로를 확인할 수 없습니다: {changed_path}"
            ) from error
        if file_info is not None and not stat.S_ISREG(file_info.st_mode):
            raise PlannerValidationError(
                f"Planner ExecPlan은 symlink가 아닌 일반 파일이어야 합니다: {changed_path}"
            )
        if file_info is None:
            plan_paths.append(changed_path)
            continue
        try:
            planner_text = plan_file.read_text(encoding="utf-8")
            frontmatter = execplan_catalog.parse_frontmatter(planner_text)
            existing_plan_id = frontmatter.get("id")
            parsed = effects._parse_execplan(
                worktree,
                plan_file,
                require_priority=not existed,
                allow_archive_fallback=False,
                allow_id_fallback=(
                    isinstance(existing_plan_id, str)
                    and existing_plan_id in snapshot.by_id
                ),
            )
        except (UnicodeError, RuntimeError) as error:
            if isinstance(error, (ExternalObservationError, GitLockError)):
                raise
            raise PlannerValidationError(
                f"Planner ExecPlan을 읽을 수 없습니다: {changed_path} ({error})"
            ) from error
        from loop_manager_internal.storage import verify_input_requests
        if any(str(source).startswith("manager:") for source in parsed["sources"]):
            if connection is None or repo is None:
                raise PlannerValidationError("Manager source requires registered request context")
            registered = connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, parsed["id"]),
            ).fetchone()
            try:
                verify_input_requests.validate(
                    connection, repo, planner_text, parsed["sources"],
                    is_new=(not existed and registered is None
                            and parsed["id"] not in snapshot.by_id),
                    metadata=parsed,
                )
            except RuntimeError as error:
                raise PlannerValidationError(str(error)) from error
        current_plans[changed_path] = parsed
        if parsed["id"] in resolutions:
            parsed["_draft_resolution"] = resolutions[parsed["id"]]
        plan_paths.append(changed_path)

    try:
        from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
        held_ids = (catalog_held_plan_ids(connection, repo)
                    if connection is not None and repo is not None else frozenset())
        from loop_manager_internal.catalog import remote_deletions
        changed_ids = {str(plan["id"]) for plan in current_plans.values()}
        changed_ids.update(snapshot.by_path[path].plan_id for path in base_paths if path not in current_plans)
        recovery_ids = {key for raw in snapshot.recovery_goals for key in json.loads(raw)["affected_ids"]} & snapshot.by_id.keys() & changed_ids
        original_dependencies = {}
        if held_ids:
            original_dependencies = {
                str(row["plan_id"]): effects._stored_string_list(row["depends_on"], "depends_on")
                for row in connection.execute(
                    "SELECT plan_id, depends_on FROM exec_plans WHERE repo = ?", (repo,),
                ).fetchall()
            }
            original_dependencies.update({
                plan.plan_id: tuple(plan.depends_on) for plan in snapshot.plans
            })
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
                try:
                    if plan_id not in resolutions:
                        execplan_drafts.validate_agent_change(parsed, path_identity.text)
                except RuntimeError as error:
                    raise PlannerValidationError(str(error)) from error
                revisions.append(PlanRevision(path, parsed, path_identity))
                continue
            original = snapshot.by_id.get(plan_id)
            if original is not None and original.path in displaced_paths:
                if plan_id in renamed_ids:
                    raise RuntimeError(
                        f"ExecPlan rename identity가 모호합니다: {plan_id}"
                    )
                try:
                    execplan_drafts.validate_agent_change(parsed, original.text)
                except RuntimeError as error:
                    raise PlannerValidationError(str(error)) from error
                revisions.append(PlanRevision(path, parsed, original))
                renamed_ids.add(plan_id)
            else:
                try:
                    execplan_drafts.validate_agent_change(parsed)
                except RuntimeError as error:
                    raise PlannerValidationError(str(error)) from error
                plans.append((path, parsed))

        new_source_paths = {path for path, _plan in plans}
        reserved_before_normalization, existing_plan_ids = _planning_id_inventory(
            worktree, new_source_paths, snapshot, connection, repo, held_ids
        )
        if plans:
            plans, id_allocations = _normalize_new_plan_batch(
                worktree, plans, reserved_before_normalization, existing_plan_ids,
                effects._parse_execplan,
                related_plans=tuple(
                    (revision.path, dict(revision.plan)) for revision in revisions
                ),
            )
        else:
            id_allocations = ()
        if id_allocations:
            for path in new_source_paths:
                current_plans.pop(path, None)
            current_plans.update(plans)
            planned_files = sorted(
                (set(planned_files) - new_source_paths)
                | {path for path, _plan in plans}
            )
            normalized_revisions: list[PlanRevision] = []
            for revision in revisions:
                try:
                    parsed = effects._parse_execplan(
                        worktree, worktree / revision.path,
                        require_priority=False, allow_archive_fallback=False,
                        allow_id_fallback=True,
                    )
                except RuntimeError as error:
                    if isinstance(error, (ExternalObservationError, GitLockError)):
                        raise
                    raise PlannerValidationError(
                        f"정규화된 ExecPlan을 다시 읽을 수 없습니다: {revision.path} ({error})"
                    ) from error
                current_plans[revision.path] = parsed
                normalized_revisions.append(
                    PlanRevision(revision.path, parsed, revision.snapshot)
                )
            revisions = normalized_revisions
        if connection is not None:
            from loop_manager_internal.storage import verify_input_requests
            for _path, plan in plans:
                sources = plan.get("sources", ())
                if any(str(source).startswith("manager:") for source in sources):
                    try:
                        verify_input_requests.validate_new_priority(
                            sources, plan.get("priority"),
                        )
                    except RuntimeError as error:
                        raise PlannerValidationError(str(error)) from error
        planned_ids = frozenset(str(plan["id"]) for plan in current_plans.values())
        if held_ids:
            # Validate references first; the held snapshot below pins the
            # registered edges before the final combined graph is checked.
            project_catalog_refresh.assert_planning_dependency_references(
                {}, current_plans.items(), held_ids,
                original_dependencies=original_dependencies,
                check_cycles=False,
            )

        if held_ids:
            local = project_catalog_refresh.validated_held_planning_snapshot(
                connection, repo, worktree, active_sha, git=effects.git,
                load_snapshot=execplan_catalog.load_execplan_snapshot,
                mutable_paths=frozenset(plan.path for plan in snapshot.plans if plan.plan_id in recovery_ids),
                recovery_mode=bool(snapshot.recovery_goals), planned_ids=planned_ids)
            catalog = local.active_metadata()
        else:
            catalog = effects.load_execplan_catalog(worktree)
        catalog.update({str(plan["id"]): plan for plan in current_plans.values()})
        if connection is not None and repo is not None:
            from loop_manager_internal.storage import execplan_draft_state
            execplan_draft_state.validate_questions(connection, repo, catalog)
        else:
            execplan_drafts.validate_questions(catalog)
        project_catalog_refresh.assert_planning_dependency_references(
            catalog, current_plans.items(), held_ids,
            original_dependencies=original_dependencies,
            withheld_ids=local.withheld_ids if held_ids else ())
        remote_deletions.validate_recovery(worktree, snapshot, current_plans, catalog, connection=connection, repo=repo)

        for path in sorted(displaced_paths):
            original = snapshot.by_path[path]
            if original.execution_state and json.loads(original.execution_state)["ever_started"] and original.plan_id not in withdrawals:
                raise RuntimeError("시작한 계획의 ID와 작업 경로를 보존해야 합니다.")
            if original.plan_id not in renamed_ids:
                if execplan_catalog.parse_frontmatter(original.text).get("draft") == "true":
                    raise RuntimeError("사용자 판단 대기 draft를 삭제할 수 없습니다.")
                deletions.append(PlanDeletion(original, withdrawals.get(original.plan_id)))

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
        reserved_ids, _existing_plan_ids = _planning_id_inventory(
            worktree, set(new_plan_paths), snapshot, connection, repo, held_ids
        )
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
            id_allocations=id_allocations,
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
    *, allow_started: bool = False,
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
    if not allow_started and (not effects.is_unowned_ready(row) or owned):
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
        execution_state=planning_execution_state(row) if allow_started else None,
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
    *, allow_started: bool = False,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    contents = _ready_revision_contents(effects.git_bytes, repository, revision, path)
    snapshot = effects._ready_plan_snapshot_from_contents(row, contents, allow_started=allow_started)
    effects._assert_ready_plan_revision(repository, revision, snapshot, contents)
    return snapshot


@dataclass(frozen=True)
class RowIsMutableReadyEffects:
    """Named effects for _row_is_mutable_ready; no entry-point/module access."""
    is_unowned_ready: Callable[..., object]


def _row_is_mutable_ready(effects: RowIsMutableReadyEffects, row: sqlite3.Row) -> bool:
    """Keep the general Planner boundary conservative and evidence based."""

    from loop_manager_internal.catalog.remote_deletions import revisable_row
    return revisable_row(row)


def planning_execution_state(row) -> str:
    """rename 중간 경로 외의 실행·선언 원본을 모두 CAS에 결합한다."""
    return json.dumps({field: row[field] for field in row.keys() if field != "path"},
                      ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class MutableReadySnapshotEffects:
    """Named effects for mutable_ready_snapshot; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    _ready_plan_snapshot_at_revision: Callable[..., object]
    _row_is_mutable_ready: Callable[..., object]
    mutable_snapshot_token: Callable[..., object]
    pr_for_branch: Callable[..., object] | None = None


def mutable_ready_snapshot(
    effects: MutableReadySnapshotEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MutableReadySnapshot:
    """PR 전 선언과 보존할 실행 상태를 Git·SQLite 원본에 결합한다."""

    effects._ensure_execplans_schema(connection)
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    project = connection.execute('SELECT planning_resume_plan_id, phase FROM projects WHERE repo = ?', (repo,)).fetchone()
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE and not (project and project[0]):
        if project and project[1] == 'PLANNING' and cycle.phase in {
                maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                maintenance_state.MaintenancePhase.REFACTORING}:
            from loop_manager_internal.catalog.remote_deletions import recovery_goals
            goals = recovery_goals(connection, repo)
            return MutableReadySnapshot(repo, active_sha, (),
                effects.mutable_snapshot_token(repo, active_sha, (), goals), goals)
        raise PlannerValidationError(
            "활성 maintenance cycle 중에는 일반 mutable READY snapshot을 만들 수 없습니다."
        )
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status IN "
        "('READY', 'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING', 'PLAN_BLOCKED') "
        "ORDER BY plan_id",
        (repo,),
    ).fetchall()
    plans: list[ReadyPlanSnapshot] = []
    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    held = catalog_held_plan_ids(connection, repo)
    from loop_manager_internal.catalog import remote_deletions
    recovery = remote_deletions.recoverable_ids(connection, repo, held)
    goals = remote_deletions.recovery_goals(connection, repo)
    manual_owned = set()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_members'").fetchone():
        manual_owned = {str(item[0]) for item in connection.execute(
            "SELECT m.plan_id FROM manual_work_members m JOIN manual_work_groups g "
            "ON g.repo=m.repo AND g.group_id=m.group_id WHERE m.repo=? "
            "AND g.state NOT IN ('MERGED','PREPARATION_CONFLICT')", (repo,))}
    for row in rows:
        if (str(row['plan_id']) in manual_owned or str(row["plan_id"]) in held - recovery
                or not effects._row_is_mutable_ready(row)):
            continue
        if str(row["plan_id"]) in recovery and not (worktree / str(row["path"])).is_file():
            continue
        from loop_manager_internal.storage import work_claims
        try:
            manual_state, manual_plan_owned = work_claims.manual_plan_ownership(
                connection, repo, str(row['plan_id']), row
            )
        except runtime_state.RuntimeStateError:
            # Incomplete related ownership evidence protects this plan alone.
            continue
        if manual_plan_owned:
            continue
        if row["branch"]:
            if effects.pr_for_branch is None:
                raise PlannerValidationError("기존 branch의 실제 PR 등록 여부를 확인할 수 없습니다.")
            if effects.pr_for_branch(Path(repo), str(row["branch"])) is not None:
                continue
        try:
            plan = effects._ready_plan_snapshot_at_revision(
                worktree, active_sha, row, allow_started=True
            )
            plans.append(replace(plan, manual_ownership_state=manual_state))
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
    pr_for_branch: Callable[..., object] | None = None


def _assert_mutable_ready_snapshot_current(
    effects: AssertMutableReadySnapshotCurrentEffects, connection: sqlite3.Connection,
    snapshot: MutableReadySnapshot,
) -> None:
    """Compare-and-snapshot guard used immediately before publication."""
    from loop_manager_internal.catalog.remote_deletions import recovery_goals
    if snapshot.recovery_goals != recovery_goals(connection, snapshot.repo):
        raise PlannerValidationError("삭제 목표 감사가 mutable READY snapshot 이후 변경됐습니다.")

    project = connection.execute('SELECT planning_resume_plan_id, phase FROM projects WHERE repo = ?', (snapshot.repo,)).fetchone()
    cycle = maintenance_state.MaintenanceRepository(connection).get(snapshot.repo)
    check_only = (project and project[1] == 'PLANNING' and not snapshot.plans
                  and cycle.phase in {maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                                      maintenance_state.MaintenancePhase.REFACTORING})
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE and not (project and project[0]) and not check_only:
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
        if plan.execution_state is not None:
            if planning_execution_state(row) != plan.execution_state:
                raise PlannerValidationError(f"mutable READY snapshot 이후 실행 상태가 변경됐습니다: {plan.plan_id}")
        elif (str(row['status']) != 'READY' or bool(row['ever_started'])
                or row['branch'] or row['worktree'] or row['workspace_owned'] or row['retry_count']
                or row['worker_session_id'] or row['reviewer_session_id']
                or any(row[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS)
                or any(row[field] != '{}' for field in
                       ('verify_failure_history', 'review_failure_history', 'review_level_history'))):
            raise PlannerValidationError(f"기존 미착수 snapshot의 실행 경계가 변경됐습니다: {plan.plan_id}")
        from loop_manager_internal.storage import work_claims
        manual_state, manual_owned = work_claims.manual_plan_ownership(
            connection, snapshot.repo, plan.plan_id, row
        )
        if manual_owned:
            raise PlannerValidationError(
                f'수동 소유 계획은 mutable READY snapshot으로 개정할 수 없습니다: {plan.plan_id}'
            )
        expected_manual_state = (
            plan.manual_ownership_state
            if plan.manual_ownership_state is not None
            else work_claims.empty_manual_ownership_state()
        )
        if manual_state != expected_manual_state:
            raise PlannerValidationError(
                f'수동 소유권·membership이 mutable READY snapshot 이후 변경됐습니다: {plan.plan_id}'
            )
        if row["branch"]:
            if effects.pr_for_branch is None:
                raise PlannerValidationError(f"계획 갱신 전에 PR이 등록됐거나 확인할 수 없습니다: {plan.plan_id}")
            if effects.pr_for_branch(Path(snapshot.repo), str(row["branch"])) is not None:
                raise MutableReadyPRConflict(f"mutable READY snapshot 이후 PR이 등록됐습니다: {plan.plan_id}")
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
