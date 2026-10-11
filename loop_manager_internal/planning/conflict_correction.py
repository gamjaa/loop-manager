"""Scoped Planner correction and Git resumption for planning conflicts."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import stat

from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn
from loop_manager_internal.planning.planning_contracts import (
    PlanningAgentPort, PlanningEventPort, PlanningGitPort,
)
from loop_manager_internal.planning.planning_rebase import rebase_in_progress


class PlanningConflictCorrection:
    """Correct owned conflicts without workspace or catalog state access."""

    def __init__(
        self, git: PlanningGitPort, agent: PlanningAgentPort, events: PlanningEventPort,
    ):
        self._git = git
        self._agent = agent
        self._events = events

    def resolve(
        self, repo: str, worktree: Path, target: str, detail: str,
        role: str, session_id: str | None, save_session: Callable[[str], None],
        collision_paths: tuple[str, ...] = (), stash_sha: str = "",
    ) -> bool:
        saved = self._git.planning_correction(repo, worktree, target)
        if saved is not None:
            paths, was_rebase = saved
            return self.finish(repo, worktree, target, paths, was_rebase)
        unmerged = tuple(sorted(filter(None, set(self._git.run(
            worktree, "diff", "--name-only", "--diff-filter=U", "-z",
        ).stdout.split("\0")))))
        paths = tuple(sorted(set(unmerged) | set(collision_paths)))
        if not paths:
            return False
        from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
        if role == "maintenance_planner" and ARTIFACT_PATH in paths:
            try:
                self._git.restore_code_map_conflict(
                    repo, worktree, stash_sha if ARTIFACT_PATH in collision_paths else "",
                )
            except RuntimeError as error:
                self._events.error(str(error))
                return False
            paths = tuple(path for path in paths if path != ARTIFACT_PATH)
            collision_paths = tuple(path for path in collision_paths if path != ARTIFACT_PATH)
            unmerged = tuple(sorted(set(unmerged) | {ARTIFACT_PATH}))
            if not paths:
                was_rebase = rebase_in_progress(self._git.run, worktree)
                self._git.save_planning_correction(repo, worktree, target, unmerged, was_rebase)
                return self.finish(repo, worktree, target, unmerged, was_rebase)
        if any(
            path != "docs/FUTURE_IDEAS.md"
            and re.fullmatch(r"docs/plans/[^/]+\.md", path) is None
            for path in paths
        ):
            self._events.error(
                "planning 역할 밖의 rebase 충돌 파일을 보존합니다: "
                + ", ".join(paths)
            )
            return False
        was_rebase = rebase_in_progress(self._git.run, worktree)
        prompt = (
            "관리 planning rebase 충돌을 같은 권한 안에서 해결하라. "
            "아래 파일의 충돌 내용을 편집하되 Git stage·continue·abort는 실행하지 마라. "
            "기존 계획 목표와 현재 원격 선언을 모두 확인하라.\n"
            + "\n".join(paths) + "\n" + detail
        )
        if stash_sha:
            prompt += (
                "\n보존된 staged 원본은 "
                f"`git show {stash_sha}^2:<경로>`, 최종 작업 원본은 "
                f"`git show {stash_sha}:<경로>`로 확인하고 모두 보존해 해결하라."
            )
        if collision_paths:
            prompt += (
                "\n보존 stash의 untracked 파일이 원격의 같은 경로와 충돌했다. "
                "원격 파일을 덮어쓰지 말고 각 새 ExecPlan을 별도 ID·파일로 보존해 교정하라. "
                "FUTURE_IDEAS는 두 내용을 현재 파일에 병합하라. 보존된 원본은 "
                f"`git show {stash_sha}^3:<경로>`로 확인할 수 있다."
            )
        result, scope = run_scoped_planner_turn(
            self._agent, repo, role, worktree, prompt, session_id, save_session,
            purpose="conflict_resolution", target=target,
        )
        if scope.error:
            self._events.error(scope.error)
            return False
        if result.code:
            self._events.error(result.message)
            return False
        for path in paths:
            file = worktree / path
            try:
                if file.is_symlink() or file.exists() and (
                    not stat.S_ISREG(file.lstat().st_mode)
                    or any(marker in file.read_bytes() for marker in (
                        b"<<<<<<< ", b"\n=======\n", b">>>>>>> ",
                    ))
                ):
                    self._events.error(f"planning 충돌 표식 또는 잘못된 파일 형식: {path}")
                    return False
            except OSError as error:
                self._events.error(f"planning 충돌 파일을 확인할 수 없습니다: {path}: {error}")
                return False
        if collision_paths:
            saved_paths = set(self._git.run(
                worktree, "ls-tree", "-r", "--name-only", f"{stash_sha}^3",
            ).stdout.splitlines())
            candidate_paths = set(self._git.run(
                worktree, "ls-files", "--others", "--exclude-standard",
            ).stdout.splitlines()) | set(self._git.run(
                worktree, "diff", "--cached", "--name-only",
            ).stdout.splitlines())
            corrected_plans = {
                path for path in candidate_paths - saved_paths
                if re.fullmatch(r"docs/plans/[^/]+\.md", path)
                and path not in collision_paths
            }
            plan_collisions = [
                path for path in collision_paths if path.startswith("docs/plans/")
            ]
            if any(self._git.run(
                worktree, "diff", "--quiet", "HEAD", "--", path,
                check=False,
            ).returncode != 0 for path in plan_collisions):
                self._events.error("새 ID 충돌 교정은 원격 ExecPlan을 보존해야 합니다.")
                return False
            if len(corrected_plans) < len(plan_collisions):
                self._events.error(
                    "원격 ID와 충돌한 새 ExecPlan을 별도 파일로 보존해야 합니다."
                )
                return False
            if "docs/FUTURE_IDEAS.md" in collision_paths and self._git.run(
                worktree, "diff", "--quiet", "HEAD", "--", "docs/FUTURE_IDEAS.md",
                check=False,
            ).returncode != 1:
                self._events.error("FUTURE_IDEAS 충돌 내용을 병합해야 합니다.")
                return False
        self._git.save_planning_correction(
            repo, worktree, target, unmerged, was_rebase,
        )
        return self.finish(repo, worktree, target, unmerged, was_rebase)

    def finish(
        self, repo: str, worktree: Path, target: str,
        paths: tuple[str, ...], was_rebase: bool,
    ) -> bool:
        if paths:
            self._git.run(worktree, "add", "--", *paths)
        in_rebase = rebase_in_progress(self._git.run, worktree)
        if in_rebase:
            continued = self._git.run(
                worktree, "-c", "core.editor=true", "rebase", "--continue",
                check=False,
            )
            if continued.returncode:
                self._events.error(continued.stdout[-2000:])
                return False
        elif not was_rebase:
            self._git.accept_resolved_stash_conflict(
                repo, worktree, target,
            )
        return True

