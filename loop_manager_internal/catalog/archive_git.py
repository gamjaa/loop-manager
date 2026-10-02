"""Detached archive Git effects with content and identity verification."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Sequence
from loop_manager_internal.catalog import execplan_archive
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.storage import execplan_archive_state
from pathlib import Path
import hashlib
import os
import tempfile


@dataclass(frozen=True)
class ArchiveGitEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _recorded_base_fetch: Callable[..., object]
    _validate_managed_path_boundary: Callable[..., object]
    command: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    is_git_lock_failure: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


class ExecPlanArchiveGitPort:
    """Concrete detached-worktree Git adapter for archive housekeeping."""

    def __init__(self, repo_path: Path, state_dir: Path, repo: str, *, effects: Callable[[], ArchiveGitEffects]):
        self._effects = effects
        self.repo_path = repo_path
        self.state_dir = state_dir
        self.repo = repo

    def fetch_remote_base(self, base_branch: str) -> str:
        return self.effects._recorded_base_fetch(
            self.repo_path, self.state_dir, base_branch, owner="project",
        )

    @staticmethod
    def _parse_tree_record(record: bytes) -> tuple[str, str, str, str]:
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("ExecPlan archive Git tree 항목 형식이 올바르지 않습니다.")
        mode, object_type, object_id = parts
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("ExecPlan archive Git 경로가 UTF-8이 아닙니다.") from error
        return (
            mode.decode("ascii"),
            object_type.decode("ascii"),
            object_id.decode("ascii"),
            path,
        )

    def _manifest(self, revision: str) -> dict[str, tuple[str, str, str]]:
        raw = self.effects.git_bytes(
            self.repo_path,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
        )
        manifest: dict[str, tuple[str, str, str]] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            mode, object_type, object_id, path = self._parse_tree_record(record)
            manifest[path] = (mode, object_type, object_id)
        return manifest

    def _entry(
        self, revision: str, path: str
    ) -> execplan_archive.ArchiveTreeEntry | None:
        raw = self.effects.git_bytes(
            self.repo_path,
            "ls-tree",
            "-z",
            revision,
            "--",
            f":(literal){path}",
        )
        records = [record for record in raw.split(b"\0") if record]
        exact = []
        for record in records:
            mode, object_type, object_id, observed = self._parse_tree_record(record)
            if observed == path:
                exact.append((mode, object_type, object_id))
        if not exact:
            return None
        if len(exact) != 1:
            raise RuntimeError(f"ExecPlan archive 경로가 중복 관측됐습니다: {path}")
        mode, object_type, object_id = exact[0]
        contents = (
            self.effects.git_bytes(self.repo_path, "cat-file", "blob", object_id)
            if object_type == "blob"
            else b""
        )
        return execplan_archive.ArchiveTreeEntry(
            mode,
            object_type,
            object_id,
            execplan_catalog.plan_content_hash(contents),
        )

    def definitions_current(
        self,
        base_sha: str,
        candidates: Sequence[execplan_archive_state.MergedArchiveCandidate],
    ) -> bool:
        for candidate in candidates:
            source = self._entry(base_sha, candidate.active_path)
            destination = self._entry(base_sha, candidate.archive_path)
            observed = source if source is not None else destination
            if observed is None or not observed.regular:
                return False
            if observed.content_hash != candidate.content_hash:
                return False
        return True

    def observe(
        self,
        revision: str,
        candidates: Sequence[execplan_archive_state.MergedArchiveCandidate],
    ) -> tuple[execplan_archive.ArchiveCandidateObservation, ...]:
        conflicts = execplan_catalog.conflicting_archived_plan_paths(
            self._manifest(revision)
        )
        return tuple(
            execplan_archive.ArchiveCandidateObservation(
                candidate,
                self._entry(revision, candidate.active_path),
                self._entry(revision, candidate.archive_path),
                next(
                    (
                        path for path in conflicts.get(candidate.plan_id, ())
                        if path != candidate.archive_path
                    ),
                    None,
                ),
            )
            for candidate in candidates
        )

    def _workspace(self, operation_id: str) -> Path:
        project_id = hashlib.sha256(self.repo.encode("utf-8")).hexdigest()[:16]
        workspace = (
            self.state_dir
            / "worktrees"
            / project_id
            / "execplan-archive"
            / operation_id
        )
        self.effects._validate_managed_path_boundary(
            self.state_dir,
            workspace,
            workspace,
            "ExecPlan archive worktree",
        )
        return workspace

    @staticmethod
    def _expected_moves(
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((move.source, move.destination) for move in moves))

    def _validate_archive_tree(
        self,
        base_sha: str,
        revision: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> str:
        before = self._manifest(base_sha)
        after = self._manifest(revision)
        classification = execplan_catalog.classify_archive_only_tree_change(
            before, after
        )
        actual = tuple(
            sorted((move.source, move.destination) for move in classification.moves)
        )
        if not classification.execplan_only or actual != self._expected_moves(moves):
            raise RuntimeError(
                "archive commit diff가 검증된 ExecPlan rename-only 집합과 다릅니다."
            )
        return self.effects.git(
            self.repo_path, "rev-parse", f"{revision}^{{tree}}"
        ).stdout.strip()

    def _prepared_from_commit(
        self,
        base_sha: str,
        operation_id: str,
        commit_oid: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> execplan_archive.PreparedArchive:
        parents = self.effects.git(
            self.repo_path,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit_oid,
        ).stdout.split()
        if len(parents) != 2 or parents[1] != base_sha:
            raise RuntimeError("archive commit은 원격 base의 단일 자식이어야 합니다.")
        final_hash = self._validate_archive_tree(base_sha, commit_oid, moves)
        return execplan_archive.PreparedArchive(
            operation_id, base_sha, final_hash, commit_oid
        )

    def _remove_clean_workspace(self, workspace: Path) -> None:
        if not (workspace.exists() or workspace.is_symlink()):
            return
        self.effects.validate_managed_worktree(self.repo_path, workspace, None)
        status = self.effects.git(workspace, "status", "--porcelain=v1", "-z").stdout
        if status:
            raise RuntimeError(
                f"archive 임시 worktree에 예상하지 못한 변경이 남았습니다: {workspace}"
            )
        removed = self.effects.git(
            self.repo_path,
            "worktree",
            "remove",
            str(workspace),
            check=False,
        )
        if removed.returncode:
            raise RuntimeError(
                "archive 임시 worktree 제거 실패:\n" + removed.stdout[-6000:]
            )

    def _discard_staged_workspace(
        self,
        workspace: Path,
        base_sha: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> None:
        """Remove only a proven Manager-owned archive staging workspace."""

        self.effects.validate_managed_worktree(self.repo_path, workspace, None)
        head = self.effects.git(workspace, "rev-parse", "HEAD").stdout.strip()
        if head != base_sha:
            raise RuntimeError(
                "archive base-race worktree HEAD가 시작 base와 다릅니다."
            )
        staged_tree = self.effects.git(workspace, "write-tree").stdout.strip()
        self._validate_archive_tree(base_sha, staged_tree, moves)
        unstaged = self.effects.git(
            workspace, "diff", "--name-only", "-z", "--"
        ).stdout
        untracked = self.effects.git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        if unstaged or untracked:
            raise RuntimeError(
                "archive base-race worktree에 검증되지 않은 파일 변경이 있습니다."
            )
        for move in reversed(moves):
            self.effects.git(workspace, "mv", "--", move.destination, move.source)
        restored_tree = self.effects.git(workspace, "write-tree").stdout.strip()
        base_tree = self.effects.git(
            self.repo_path, "rev-parse", f"{base_sha}^{{tree}}"
        ).stdout.strip()
        if restored_tree != base_tree:
            raise RuntimeError(
                "archive base-race worktree를 원래 tree로 복원하지 못했습니다."
            )
        self._remove_clean_workspace(workspace)

    def _stage_archive_moves(
        self,
        workspace: Path,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> None:
        """Stage recovered renames without requiring deleted paths to exist."""

        for move in moves:
            self.effects.git(
                workspace,
                "update-index",
                "--force-remove",
                "--",
                move.source,
            )
        destinations = [
            f":(literal){move.destination}" for move in moves
        ]
        self.effects.git(workspace, "add", "--", *destinations)

    def _commit_archive(self, workspace: Path, message: str) -> None:
        """Create a Manager-owned commit without ambient identity or hooks."""

        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Loop Manager",
                "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
                "GIT_COMMITTER_NAME": "Loop Manager",
                "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-execplan-archive-hooks-"
        ) as hooks_directory:
            committed = self.effects.command(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={hooks_directory}",
                    "-c",
                    "commit.gpgSign=false",
                    "-C",
                    str(workspace),
                    "commit",
                    "-m",
                    message,
                ],
                check=False,
                env=environment,
            )
        if committed.returncode:
            output = committed.stdout[-6000:].strip()
            detail = f"archive commit 실패:\n{output}"
            if self.effects.is_git_lock_failure(output):
                raise GitLockError(detail)
            raise RuntimeError(detail)

    def prepare_commit(
        self,
        base_sha: str,
        base_branch: str,
        operation_id: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> execplan_archive.PreparedArchive:
        workspace = self._workspace(operation_id)
        if workspace.exists() or workspace.is_symlink():
            self.effects.validate_managed_worktree(self.repo_path, workspace, None)
            head = self.effects.git(workspace, "rev-parse", "HEAD").stdout.strip()
            if head != base_sha:
                return self._prepared_from_commit(
                    base_sha, operation_id, head, moves
                )
        else:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self.effects.git(self.repo_path, "worktree", "prune", "--expire", "now")
            self.effects.git(
                self.repo_path,
                "worktree",
                "add",
                "--detach",
                str(workspace),
                base_sha,
            )
            self.effects.validate_managed_worktree(self.repo_path, workspace, None)

        for move in moves:
            source = workspace / move.source
            destination = workspace / move.destination
            source_present = source.exists() or source.is_symlink()
            destination_present = destination.exists() or destination.is_symlink()
            if source_present and not destination_present:
                destination.parent.mkdir(parents=True, exist_ok=True)
                self.effects.git(workspace, "mv", "--", move.source, move.destination)
            elif not source_present and destination_present:
                continue
            else:
                raise RuntimeError(
                    "archive 임시 worktree의 rename 상태가 불확실합니다: "
                    f"{move.source} -> {move.destination}"
                )

        self._stage_archive_moves(workspace, moves)
        staged_tree = self.effects.git(workspace, "write-tree").stdout.strip()
        self._validate_archive_tree(base_sha, staged_tree, moves)
        if self.fetch_remote_base(base_branch) != base_sha:
            self._discard_staged_workspace(workspace, base_sha, moves)
            raise execplan_archive.ArchiveRemoteBaseChanged(
                "archive commit 직전 원격 base가 변경됐습니다."
            )
        plan_ids = ", ".join(sorted(move.plan_id for move in moves))
        self._commit_archive(
            workspace, f"Archive merged ExecPlans: {plan_ids}"
        )
        commit_oid = self.effects.git(workspace, "rev-parse", "HEAD").stdout.strip()
        prepared = self._prepared_from_commit(
            base_sha, operation_id, commit_oid, moves
        )
        return prepared

    def commit_matches(
        self,
        prepared: execplan_archive.PreparedArchive,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> bool:
        exists = self.effects.git(
            self.repo_path,
            "cat-file",
            "-e",
            f"{prepared.commit_oid}^{{commit}}",
            check=False,
        )
        if exists.returncode:
            return False
        actual = self._prepared_from_commit(
            prepared.base_sha,
            prepared.operation_id,
            prepared.commit_oid,
            moves,
        )
        if actual.final_hash != prepared.final_hash:
            return False
        return True

    def cleanup_prepared(
        self, prepared: execplan_archive.PreparedArchive
    ) -> None:
        workspace = self._workspace(prepared.operation_id)
        if not (workspace.exists() or workspace.is_symlink()):
            return
        self.effects.validate_managed_worktree(self.repo_path, workspace, None)
        head = self.effects.git(workspace, "rev-parse", "HEAD").stdout.strip()
        if head != prepared.commit_oid:
            raise RuntimeError(
                "archive 임시 worktree HEAD가 저장된 commit과 다릅니다: "
                f"{head} != {prepared.commit_oid}"
            )
        self._remove_clean_workspace(workspace)

    def push_fast_forward(
        self,
        prepared: execplan_archive.PreparedArchive,
        base_branch: str,
    ) -> execplan_archive.ArchivePushResult:
        # Named-remote push updates tracking refs before the recorded fetch
        # can prove their transition. Use the resolved push destinations, as
        # diagnostic publication does, and let fetch perform the owned CAS.
        urls = self.effects.git(self.repo_path, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
        if not urls:
            raise RuntimeError("archive push 대상 origin URL을 확인할 수 없습니다.")
        pushes = [self.effects.git(
            self.repo_path, "push", "--", url,
            f"{prepared.commit_oid}:refs/heads/{base_branch}", check=False,
        ) for url in urls]
        try:
            remote_oid = self.fetch_remote_base(base_branch)
        except Exception as error:
            return execplan_archive.ArchivePushResult(
                False,
                error=f"push 결과 원격 확인 실패: {error}",
            )
        if remote_oid == prepared.commit_oid:
            return execplan_archive.ArchivePushResult(True, remote_oid)
        if remote_oid != prepared.base_sha:
            return execplan_archive.ArchivePushResult(
                False,
                remote_oid=remote_oid,
                error="push 도중 원격 base가 변경됐습니다.",
                base_changed=True,
            )
        return execplan_archive.ArchivePushResult(
            False,
            remote_oid=remote_oid,
            error="\n".join(pushed.stdout for pushed in pushes)[-6000:] or "fast-forward push가 거부됐습니다.",
        )

    @property
    def effects(self) -> ArchiveGitEffects:
        return self._effects()
