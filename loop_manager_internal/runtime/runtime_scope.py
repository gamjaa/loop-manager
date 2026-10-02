"""Read-only evidence for replacing a persisted runtime registration lock."""

from pathlib import Path
from typing import Callable

from loop_manager_internal.git import manager_git_journal as journal


def proven_runtime_recreation(
    state_dir: Path,
    source: Path,
    runtime: Path,
    sha: str,
    previous: tuple[str, ...] | None,
    current: tuple[str, ...] | None,
    *,
    validate_lock: Callable[[str], None],
) -> bool:
    """Require unchanged state and a successful owned removal of the old lock.

    The caller must first validate the live runtime and its creation marker.
    A receipt alone is insufficient: its exact intent and completed journal
    result must agree with the registration saved before the Agent started.
    """
    if previous is None or current is None or previous == current:
        return False
    previous_state = tuple(x for x in previous if not x.startswith("locked="))
    current_state = tuple(x for x in current if not x.startswith("locked="))
    if (
        previous_state != current_state
        or f"HEAD={sha}" not in previous
        or "detached=True" not in previous
    ):
        return False
    locks = [x.removeprefix("locked=") for x in previous if x.startswith("locked=")]
    if len(locks) != 1:
        return False
    try:
        validate_lock(locks[0])
        expected_before = {
            "exists": True,
            "registration": {"head": sha, "detached": True, "locked": locks[0]},
        }
        repo, target = str(source), str(runtime)
        slot = journal._slot(state_dir, repo, "runtime_remove", target)
        for receipt_path in (state_dir / "runtime-removals").glob("*.json"):
            receipt = journal._read(receipt_path)
            identity = receipt_path.stem
            if receipt != {
                "id": identity, "repo": repo, "target": target,
                "before": expected_before,
            }:
                continue
            intent = journal._read(slot / f"{identity}.intent.json")
            result = journal._read(slot / f"{identity}.result.json")
            if (
                intent.get("version") != 1
                or intent.get("id") != identity or intent.get("owner") != "bootstrap"
                or intent.get("repo") != repo or intent.get("target") != target
                or intent.get("kind") != "runtime_remove"
                or intent.get("before") != expected_before
                or result.get("id") != identity or result.get("status") != "complete"
            ):
                continue
            candidates = journal.verified_transition(intent, result["observed"], True)
            if candidates == result.get("candidates"):
                return True
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        return False
    return False
