"""Durable, operation-owned evidence for Manager Git mutations.

The caller holds the process lock and supplies Git observations and an action
marker (a reflog message or a worktree lock reason).  This module never infers
ownership from an OID or elapsed time alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any


class MutationEvidenceError(RuntimeError):
    """An intended Git transition cannot be attributed to the Manager."""


def verified_transition(
    intent: Mapping[str, Any], observed: Mapping[str, Any], marker: bool,
) -> dict[str, dict[str, Any]]:
    """Return only matching ref/worktree baseline candidates.

    The before/after maps are deliberately small, task-relevant observations.
    A source local delta is result evidence, never an Agent allowance.
    """
    before = intent["before"]
    expected = intent["expected"]
    kind = intent["kind"]
    target = intent["target"]
    oid = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
    if kind in {"bootstrap_fetch", "project_fetch"}:
        valid = (
            target.startswith("refs/remotes/origin/")
            and set(before) == set(expected) == {"oid"}
            and isinstance(expected["oid"], str) and oid.fullmatch(expected["oid"])
            and (before["oid"] is None or (
                isinstance(before["oid"], str) and oid.fullmatch(before["oid"])))
        )
    elif kind == "source_fast_forward":
        valid = (
            set(before) == set(expected) == {"head", "branch", "changes", "operations"}
            and isinstance(before["head"], str) and oid.fullmatch(before["head"])
            and isinstance(expected["head"], str) and oid.fullmatch(expected["head"])
            and isinstance(before["branch"], str) and before["branch"] == expected["branch"]
            and before["changes"] == expected["changes"]
            and before["operations"] == expected["operations"] == []
        )
    elif kind in {"runtime_create", "runtime_remove"}:
        valid = (
            set(before) == set(expected) == {"exists", "registration"}
            and isinstance(before["exists"], bool)
            and isinstance(expected["exists"], bool)
            and (kind != "runtime_create" or (
                before == {"exists": False, "registration": None}
                and expected["exists"] is True
                and isinstance(expected["registration"], dict)
                and isinstance(expected["registration"].get("head"), str)
                and oid.fullmatch(expected["registration"]["head"])
                and expected["registration"].get("detached") is True
                and isinstance(expected["registration"].get("locked"), str)
                and expected["registration"]["locked"].startswith("loop-manager-runtime:")
            ))
            and (kind != "runtime_remove" or (
                before["exists"] is True
                and before["registration"] is not None
                and expected == {"exists": False, "registration": None}
            ))
        )
    else:
        valid = False
    if not valid:
        raise MutationEvidenceError("Manager Git intent의 허용 전이가 유효하지 않습니다.")
    if observed != expected or (before != expected and not marker):
        raise MutationEvidenceError("Manager Git 결과 또는 작업 소유 표식이 일치하지 않습니다.")
    if kind in {"bootstrap_fetch", "project_fetch"}:
        return {"refs": {target: expected["oid"]}, "worktrees": {}}
    if kind == "runtime_create":
        return {"refs": {}, "worktrees": {target: expected["registration"]}}
    if kind == "runtime_remove":
        return {"refs": {}, "worktrees": {target: None}}
    if kind == "source_fast_forward":
        return {"refs": {}, "worktrees": {}}
    raise MutationEvidenceError(f"지원하지 않는 Manager Git 작업: {kind}")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MutationEvidenceError(f"Manager Git journal을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict):
        raise MutationEvidenceError("Manager Git journal 형식이 유효하지 않습니다.")
    return value


def _slot(state_dir: Path, repo: str, kind: str, target: str) -> Path:
    key = json.dumps([repo, kind, target], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return state_dir / "manager-git-journal" / digest


def execute(
    state_dir: Path, *, owner: str, kind: str, repo: str, target: str,
    before: Mapping[str, Any], expected: Mapping[str, Any],
    observe: Callable[[], dict[str, Any]],
    apply: Callable[[str], None],
    marker: Callable[[Mapping[str, Any]], bool],
) -> str:
    """Execute or recover one transition. Return applied, recovered, or equal.

    The immutable intent is fsynced before Git. A separate result is fsynced
    before clearing active state. An interrupted old intent with no Git effect
    is explicitly recorded as aborted before a new intent can be started.
    """
    if not owner or not repo or not target or not re.fullmatch(r"[a-z_]+", kind):
        raise MutationEvidenceError("Manager Git 작업 identity가 유효하지 않습니다.")
    root = state_dir / "manager-git-journal"
    root.mkdir(parents=True, exist_ok=True)
    _sync_directory(state_dir)
    slot = _slot(state_dir, repo, kind, target)
    slot.mkdir(parents=True, exist_ok=True)
    _sync_directory(root)
    active = slot / "active.json"
    current = observe()
    if active.exists():
        previous = _read(active)
        if (
            previous.get("version") != 1 or previous.get("repo") != repo
            or previous.get("kind") != kind or previous.get("target") != target
            or not re.fullmatch(r"[0-9a-f]{32}", str(previous.get("id", "")))
            or _read(slot / f"{previous['id']}.intent.json") != previous
        ):
            raise MutationEvidenceError("Manager Git 활성 intent가 불완전합니다.")
        if current == previous["expected"]:
            candidates = verified_transition(previous, current, marker(previous))
            _finish(slot, active, previous, current, candidates, "complete")
            if previous["owner"] == owner and previous["expected"] == expected:
                return "recovered"
        elif current == previous["before"]:
            if previous["owner"] != owner or previous["expected"] != expected:
                _finish(slot, active, previous, current, {}, "aborted")
            else:
                apply(str(previous["id"]))
                after = observe()
                candidates = verified_transition(previous, after, marker(previous))
                _finish(slot, active, previous, after, candidates, "complete")
                return "applied"
        else:
            raise MutationEvidenceError("Manager Git intent 뒤 영향권 상태가 예상 밖으로 변경됐습니다.")
        current = observe()
    if current != before:
        raise MutationEvidenceError("Manager Git 작업 직전 상태가 기대값과 다릅니다.")
    if before == expected:
        # Equal is a durable observation, with no Git write to attribute.
        outcome = "equal"
    else:
        outcome = "complete"
    identity = secrets.token_hex(16)
    intent = {
        "version": 1, "id": identity, "owner": owner, "kind": kind,
        "repo": repo, "target": target, "before": dict(before),
        "expected": dict(expected),
    }
    _write_new(slot / f"{identity}.intent.json", intent)
    _write_new(active, intent)
    if before != expected:
        apply(identity)
    after = observe()
    candidates = verified_transition(intent, after, marker(intent))
    _finish(slot, active, intent, after, candidates, outcome)
    return "equal" if before == expected else "applied"


def recover_active(
    state_dir: Path, *, repo: str, kind: str, target: str,
    observe: Callable[[], dict[str, Any]],
    marker: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """Finalize an interrupted write before a caller's early equal return."""
    slot = _slot(state_dir, repo, kind, target)
    active = slot / "active.json"
    if not active.exists():
        return False
    intent = _read(active)
    if (intent.get("repo"), intent.get("kind"), intent.get("target")) != (
        repo, kind, target,
    ) or _read(slot / f"{intent.get('id')}.intent.json") != intent:
        raise MutationEvidenceError("Manager Git 활성 intent가 불완전합니다.")
    current = observe()
    if current == intent["before"]:
        return False
    candidates = verified_transition(intent, current, marker(intent))
    _finish(slot, active, intent, current, candidates, "complete")
    return True


def _finish(
    slot: Path, active: Path, intent: Mapping[str, Any],
    observed: Mapping[str, Any], candidates: Mapping[str, Any], status: str,
) -> None:
    result_path = slot / f"{intent['id']}.result.json"
    result = {"id": intent["id"], "status": status,
              "observed": dict(observed), "candidates": dict(candidates)}
    if result_path.exists():
        if _read(result_path) != result:
            raise MutationEvidenceError("Manager Git 저장 결과가 현재 증거와 다릅니다.")
    else:
        _write_new(result_path, result)
    active.unlink()
    _sync_directory(slot)


def verified_candidates(
    state_dir: Path, repo: str, *,
    baseline_refs: Mapping[str, Any], baseline_worktrees: Mapping[str, Any],
    live_refs: Mapping[str, Any], live_worktrees: Mapping[str, Any],
    marker: Callable[[Mapping[str, Any]], bool],
) -> dict[str, dict[str, Any]]:
    """Offer only unbroken, owned transitions from a caller's baseline to Git."""
    root = state_dir / "manager-git-journal"
    candidates: dict[str, dict[str, Any]] = {"refs": {}, "worktrees": {}}
    if not root.exists():
        return candidates
    transitions: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    active_targets: set[tuple[str, str]] = set()
    for slot in root.iterdir():
        if not slot.is_dir():
            continue
        active = slot / "active.json"
        if active.exists():
            intent = _read(active)
            category = "refs" if intent.get("kind") in {
                "bootstrap_fetch", "project_fetch",
            } else "worktrees"
            active_targets.add((category, str(intent.get("target"))))
        for result_path in slot.glob("*.result.json"):
            result = _read(result_path)
            if result.get("status") != "complete":
                continue
            identity = str(result.get("id", ""))
            intent = _read(slot / f"{identity}.intent.json")
            if intent.get("repo") != repo:
                continue
            validated = verified_transition(
                intent, result["observed"], marker(intent),
            )
            if validated != result["candidates"]:
                raise MutationEvidenceError("Manager Git 저장된 기준선 후보가 불일치합니다.")
            for category in candidates:
                for target, value in validated.get(category, {}).items():
                    before = intent["before"]
                    prior = before["oid"] if category == "refs" else before["registration"]
                    transitions.setdefault((category, target), []).append((prior, value))
    for (category, target), steps in transitions.items():
        if (category, target) in active_targets:
            continue
        baseline = baseline_refs if category == "refs" else baseline_worktrees
        live = live_refs if category == "refs" else live_worktrees
        cursor = baseline.get(target)
        wanted = live.get(target)
        if cursor == wanted:
            continue
        seen: set[str] = set()
        remaining = steps[:]
        while True:
            key = json.dumps(cursor, sort_keys=True)
            if key in seen:
                break
            seen.add(key)
            next_values = [after for before, after in remaining if before == cursor]
            if not next_values or any(value != next_values[0] for value in next_values):
                break
            value = next_values[0]
            remaining = [(before, after) for before, after in remaining
                         if not (before == cursor and after == value)]
            cursor = value
            if cursor == wanted:
                # A later owned transition out of this state means a live
                # reversion cannot be attributed to this earlier result.
                if not any(before == cursor for before, _after in remaining):
                    candidates[category][target] = cursor
                break
    return candidates
