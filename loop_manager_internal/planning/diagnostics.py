"""planning diagnostics boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import sqlite3


def planning_diagnostics_path(
    state_dir: Path,
    repo: str,
    fingerprint: str,
) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "logs" / project_id / "planning" / f"{fingerprint}.txt"


@dataclass(frozen=True)
class PreservePlanningDiagnosticsEffects:
    """Named effects for preserve_planning_diagnostics; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]
    planning_diagnostics_path: Callable[..., object]


def preserve_planning_diagnostics(
    effects: PreservePlanningDiagnosticsEffects, state_dir: Path,
    repo: str,
    row: sqlite3.Row,
) -> Path:
    fingerprint = row["planning_attempt_fingerprint"]
    if not fingerprint:
        fingerprint = row["planning_error_fingerprint"] or "unknown"
    worktree = Path(row["worktree"]) if row["worktree"] else None
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
    diff = ""
    if worktree and worktree.is_dir():
        try:
            base_revision = row["active_sha"] or "HEAD"
            diff = effects.git(worktree, "diff", "--binary", base_revision).stdout
            for relative_path in effects._nul_git_paths(
                worktree, "ls-files", "--others", "-z"
            ):
                untracked = effects.git(
                    worktree,
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    os.devnull,
                    relative_path,
                    check=False,
                )
                if untracked.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"untracked diff 생성 실패 ({relative_path}): "
                        f"git 종료 코드 {untracked.returncode}"
                    )
                diff += untracked.stdout
        except RuntimeError as diff_error:
            diff = f"diff 보존 실패: {diff_error}"
    # Preserve each adopted base separately; repeated corrections must not
    # overwrite the original failed result or its retry evidence.
    identity = hashlib.sha256(
        f"{fingerprint}\0{row['active_sha'] or 'unknown'}".encode("utf-8")
    ).hexdigest()
    diagnostic = effects.planning_diagnostics_path(state_dir, repo, identity)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "\n".join(
            [
                f"input_fingerprint: {fingerprint}",
                f"planning_worktree: {worktree or ''}",
                f"planner_session_id: {row['planner_session_id'] or ''}",
                f"validation_retries: {row['planning_retry_count']}",
                f"active_sha: {row['active_sha'] or ''}",
                f"planning_error: {row['planning_error'] or ''}",
                f"planning_error_incident_id: {row['planning_error_incident_id'] or ''}",
                f"planning_error_origin: {row['planning_error_origin'] or ''}",
                f"planning_validation_error: {row['planning_validation_error'] or ''}",
                f"planning_result_completed: {int(bool(row['planning_result_completed']))}",
                f"planning_result: {row['planning_result'] or ''}",
                "",
                "last_error:",
                str(error),
                "",
                "diff:",
                diff,
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic
