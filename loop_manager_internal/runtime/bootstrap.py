"""Bootstrap and exact-revision runtime lifecycle with explicit Git effects.

Git mutation and journal policy remain in the existing Git adapters. This
service owns runtime selection, ownership validation and child handoff only.
"""
from __future__ import annotations
from contextlib import closing

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess
import sys

from loop_manager_internal.git import manager_git_journal
from .contracts import BootstrapRefreshHandoff, RuntimeMutation, RuntimeWorktree, WorkspaceOwnershipError
from .process_lock import _held_process_lock_identity
from .runtime_identity import (
    _canonical_path, _read_runtime_intent, _record_legacy_runtime_adoption,
    _validate_legacy_runtime_adoption, _write_runtime_intent,
    expected_runtime_worktree, runtime_child_arguments,
)


@dataclass(frozen=True)
class BootstrapGitEffects:
    """Named existing effects; no entry-point or process-global lookup."""
    git: Callable
    _git_operation_entries: Callable
    _validate_managed_path_boundary: Callable
    validate_managed_worktree: Callable
    _worktree_catalog: Callable
    _ignored_worktree_paths: Callable
    _recorded_base_fetch: Callable
    github_repository: Callable
    synchronize_base_worktree: Callable
    repository_origin: Callable
    operation_log: Callable


class BootstrapService:
    def __init__(self, effects: BootstrapGitEffects, *, runtime_root: Path,
                 interpreter: str | None = None):
        self.effects = effects
        self.runtime_root = runtime_root
        self.interpreter = interpreter if interpreter is not None else sys.executable


    def validate_runtime_worktree(self,
        source: Path,
        state_dir: Path,
        runtime: Path,
        commit_sha: str,
    ) -> None:
        """Prove that an existing runtime is the clean detached worktree we own."""

        expected = expected_runtime_worktree(state_dir, source, commit_sha)
        self.effects._validate_managed_path_boundary(state_dir, runtime, expected, "runtime worktree")
        self.effects.validate_managed_worktree(source, runtime, None)
        head = self.effects.git(runtime, "rev-parse", "HEAD").stdout.strip()
        if head != commit_sha:
            raise WorkspaceOwnershipError(
                f"runtime HEAD가 선택한 origin/main과 다릅니다: {head} "
                f"(expected {commit_sha})"
            )
        status = self.effects.git(
            runtime,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        if status.strip():
            raise WorkspaceOwnershipError(
                f"기존 runtime worktree가 clean 상태가 아닙니다: {runtime}"
            )
        operations = self.effects._git_operation_entries(runtime)
        if operations:
            raise WorkspaceOwnershipError(
                "기존 runtime worktree에 Git operation이 진행 중입니다: "
                + ", ".join(sorted(operations))
            )


    def _runtime_catalog_record(self, source: Path, runtime: Path) -> dict[str, str | bool]:
        expected = _canonical_path(runtime)
        for record in self.effects._worktree_catalog(source):
            if _canonical_path(Path(str(record.get("worktree", "")))) == expected:
                return record
        raise WorkspaceOwnershipError(f"runtime Git worktree 등록이 없습니다: {runtime}")


    def _runtime_transition_state(self, source: Path, runtime: Path) -> dict[str, object]:
        expected = _canonical_path(runtime)
        record = next((item for item in self.effects._worktree_catalog(source)
                       if _canonical_path(Path(str(item.get("worktree", "")))) == expected),
                      None)
        return {
            "exists": runtime.exists() or runtime.is_symlink(),
            "registration": (
                {"head": record.get("HEAD"), "detached": bool(record.get("detached")),
                 "locked": record.get("locked")}
                if record is not None else None
            ),
        }


    def _runtime_transition_marker(self,
        state_dir: Path, source: Path, runtime: Path, sha: str,
        intent: Mapping[str, object],
    ) -> bool:
        expected = intent["expected"]
        lock_reason = expected["registration"]["locked"]
        if not isinstance(lock_reason, str) or not lock_reason.startswith("loop-manager-runtime:"):
            return False
        mutation_id = lock_reason.removeprefix("loop-manager-runtime:")
        try:
            return _read_runtime_intent(
                state_dir, mutation_id, source, runtime, sha,
            ) == (True, lock_reason)
        except RuntimeError:
            return False


    def _validate_runtime_lock_origin(self,
        state_dir: Path, source: Path, runtime: Path, sha: str, lock_reason: object,
    ) -> None:
        prefix = "loop-manager-runtime:"
        if not isinstance(lock_reason, str) or not lock_reason.startswith(prefix):
            raise RuntimeError("runtime 잠금 소유권을 확인할 수 없습니다.")
        prior_created, prior_lock = _read_runtime_intent(
            state_dir, lock_reason.removeprefix(prefix), source, runtime, sha,
        )
        if not prior_created or prior_lock != lock_reason:
            raise RuntimeError("runtime 등록 증거가 유효하지 않습니다.")


    def _validated_runtime_registration_marker(self,
        source: Path,
        state_dir: Path,
        runtime: Path,
        sha: str,
        mutation_id: str | None,
        *,
        legacy: bool,
        record_legacy_adoption: bool = False,
    ) -> str | None:
        """Verify the catalog marker against durable intent without changing Git."""
        lock_reason = self._runtime_catalog_record(source, runtime).get("locked")
        if legacy:
            if lock_reason is None:
                if record_legacy_adoption:
                    _record_legacy_runtime_adoption(state_dir, source, runtime, sha)
                else:
                    _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
                return None
            self._validate_runtime_lock_origin(state_dir, source, runtime, sha, lock_reason)
            return lock_reason

        if mutation_id is None:
            raise RuntimeError("runtime mutation ID가 없습니다.")
        created, expected_lock_reason = _read_runtime_intent(
            state_dir, mutation_id, source, runtime, sha,
        )
        if lock_reason != expected_lock_reason:
            raise RuntimeError("runtime 등록의 durable mutation 표식이 다릅니다.")
        if created and expected_lock_reason != f"loop-manager-runtime:{mutation_id}":
            raise RuntimeError("runtime 등록의 durable mutation ID가 다릅니다.")
        if not created and expected_lock_reason is None:
            _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
        elif not created:
            self._validate_runtime_lock_origin(
                state_dir, source, runtime, sha, expected_lock_reason,
            )
        return lock_reason


    def prepare_runtime_worktree(self,
        source: Path, state_dir: Path, *, mutation_id: str | None = None,
        commit_sha: str | None = None,
    ) -> RuntimeWorktree:
        """Select an immutable linked runtime worktree at a fetched commit."""

        source = _canonical_path(source)
        if commit_sha is None:
            _, commit_sha = self.fetch_bootstrap_revision(source, state_dir)
        runtime = expected_runtime_worktree(state_dir, source, commit_sha)
        self.effects._validate_managed_path_boundary(
            state_dir,
            runtime,
            runtime,
            "runtime worktree",
        )
        try:
            _canonical_path(runtime).relative_to(source)
        except ValueError:
            pass
        else:
            raise WorkspaceOwnershipError(
                f"runtime 경로가 source checkout 내부에 있습니다: {runtime}"
            )
        removal_slot = manager_git_journal._slot(
            state_dir, str(source), "runtime_remove", str(runtime),
        )
        if (removal_slot / "active.json").exists():
            self.remove_runtime_worktree(
                source, state_dir, RuntimeWorktree(runtime, commit_sha, mutation_id),
            )
        if mutation_id is not None:
            manager_git_journal.recover_active(
                state_dir, repo=str(source), kind="runtime_create", target=str(runtime),
                observe=lambda: self._runtime_transition_state(source, runtime),
                marker=lambda intent: self._runtime_transition_marker(
                    state_dir, source, runtime, commit_sha, intent,
                ),
            )
        if runtime.exists() or runtime.is_symlink():
            if not runtime.is_dir():
                raise WorkspaceOwnershipError(
                    f"runtime 경로가 디렉터리가 아닙니다: {runtime}"
                )
            self.validate_runtime_worktree(source, state_dir, runtime, commit_sha)
            if mutation_id is not None:
                record = self._runtime_catalog_record(source, runtime)
                lock_reason = record.get("locked")
                if lock_reason:
                    prefix = "loop-manager-runtime:"
                    if not isinstance(lock_reason, str) or not lock_reason.startswith(prefix):
                        raise WorkspaceOwnershipError(f"runtime 잠금 소유권을 확인할 수 없습니다: {runtime}")
                    prior_id = lock_reason.removeprefix(prefix)
                    prior_created, prior_lock = _read_runtime_intent(
                        state_dir, prior_id, source, runtime, commit_sha,
                    )
                    if not prior_created or prior_lock != lock_reason:
                        raise WorkspaceOwnershipError(f"runtime 등록 증거가 유효하지 않습니다: {runtime}")
                else:
                    _validate_legacy_runtime_adoption(state_dir, source, runtime, commit_sha)
                _write_runtime_intent(
                    state_dir, mutation_id, source, runtime, commit_sha,
                    created=False, lock_reason=lock_reason if isinstance(lock_reason, str) else None,
                )
            return RuntimeWorktree(runtime, commit_sha, mutation_id)

        expected_path = _canonical_path(runtime)
        registered = next(
            (
                record
                for record in self.effects._worktree_catalog(source)
                if _canonical_path(Path(str(record.get("worktree", ""))))
                == expected_path
            ),
            None,
        )
        if registered is not None:
            raise WorkspaceOwnershipError(
                f"runtime 경로가 없지만 Git worktree 등록이 남아 있습니다: {runtime}"
            )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if mutation_id is not None:
            lock_reason = f"loop-manager-runtime:{mutation_id}"
            before_state = self._runtime_transition_state(source, runtime)
            if before_state != {"exists": False, "registration": None}:
                raise WorkspaceOwnershipError("runtime 생성 직전 worktree 상태가 변경됐습니다.")
            expected_state = {
                "exists": True,
                "registration": {"head": commit_sha, "detached": True,
                                 "locked": lock_reason},
            }

            def apply(_token: str) -> None:
                _write_runtime_intent(
                    state_dir, mutation_id, source, runtime, commit_sha,
                    created=True, lock_reason=lock_reason,
                )
                self.effects.git(source, "worktree", "add", "--detach", "--lock",
                    f"--reason={lock_reason}", str(runtime), commit_sha)
                self.validate_runtime_worktree(source, state_dir, runtime, commit_sha)

            manager_git_journal.execute(
                state_dir, owner="bootstrap", kind="runtime_create",
                repo=str(source), target=str(runtime), before=before_state,
                expected=expected_state, observe=lambda: self._runtime_transition_state(source, runtime),
                apply=apply, marker=lambda intent: self._runtime_transition_marker(
                    state_dir, source, runtime, commit_sha, intent,
                ),
            )
        else:
            self.effects.git(source, "worktree", "add", "--detach", str(runtime), commit_sha)
        self.validate_runtime_worktree(source, state_dir, runtime, commit_sha)
        return RuntimeWorktree(runtime, commit_sha, mutation_id)


    def remove_runtime_worktree(self,
        source: Path, state_dir: Path, runtime: RuntimeWorktree,
    ) -> None:
        """Remove an inactive owned runtime under the caller's process lock.

        A missing directory is never proof of our removal: recovery requires the
        durable receipt written only after a successful non-forced Git removal.
        """
        source = _canonical_path(source)
        path = runtime.path
        sha = runtime.commit_sha
        self.effects._validate_managed_path_boundary(
            state_dir, path, expected_runtime_worktree(state_dir, source, sha),
            "runtime worktree",
        )
        receipts = state_dir / "runtime-removals"

        def marker(intent: Mapping[str, object]) -> bool:
            receipt = receipts / f"{intent['id']}.json"
            return receipt.is_file() and manager_git_journal._read(receipt) == {
                "id": intent["id"], "repo": str(source), "target": str(path),
                "before": intent["before"],
            }

        if manager_git_journal.recover_active(
            state_dir, repo=str(source), kind="runtime_remove", target=str(path),
            observe=lambda: self._runtime_transition_state(source, path), marker=marker,
        ):
            return
        self.validate_runtime_worktree(source, state_dir, path, sha)
        before = self._runtime_transition_state(source, path)
        lock_reason = before["registration"]["locked"]
        self._validate_runtime_lock_origin(state_dir, source, path, sha, lock_reason)
        # Do not let Git remove ignored user data as part of an otherwise clean tree.
        if self.effects._ignored_worktree_paths(path):
            raise WorkspaceOwnershipError("runtime ignored 파일을 보존하기 위해 정리를 중단합니다.")
        receipts.mkdir(parents=True, exist_ok=True)
        manager_git_journal._sync_directory(state_dir)

        def apply(token: str) -> None:
            self.validate_runtime_worktree(source, state_dir, path, sha)
            if self._runtime_transition_state(source, path) != before or self.effects._ignored_worktree_paths(path):
                raise WorkspaceOwnershipError("runtime 정리 직전 상태가 변경됐습니다.")
            self.effects.git(source, "worktree", "unlock", str(path))
            # An interruption after unlock or before the receipt deliberately blocks;
            # neither absence nor a stale creation marker can authorize recovery.
            self.effects.git(source, "worktree", "remove", str(path))
            manager_git_journal._write_new(receipts / f"{token}.json", {
                "id": token, "repo": str(source), "target": str(path), "before": before,
            })

        manager_git_journal.execute(
            state_dir, owner="bootstrap", kind="runtime_remove", repo=str(source),
            target=str(path), before=before,
            expected={"exists": False, "registration": None},
            observe=lambda: self._runtime_transition_state(source, path),
            apply=apply, marker=marker,
        )


    def bootstrap_base_branch(self, source: Path, state_dir: Path, origin: str) -> str:
        """Read a registered self base without changing SQLite before the handoff."""

        database = state_dir / "loop-manager.db"
        if not database.is_file():
            return "main"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            if not {"repo", "base_branch"} <= columns:
                raise RuntimeError("상태 DB의 프로젝트 등록 정보를 읽을 수 없습니다.")
            origin_column = "origin" if "origin" in columns else "NULL AS origin"
            registered_filter = "WHERE registered = 1" if "registered" in columns else ""
            rows = connection.execute(
                f"SELECT repo, base_branch, {origin_column} FROM projects {registered_filter}"
            ).fetchall()
        # A registered alias or another worktree must not select the bootstrap base.
        matches = [row for row in rows if row[0] == str(source)]
        if len(matches) > 1:
            raise RuntimeError("등록된 self source identity가 중복됩니다.")
        if not matches:
            return "main"
        _, base, saved_origin = matches[0]
        if saved_origin is not None and str(saved_origin) != origin:
            raise RuntimeError("등록된 self origin과 현재 source origin이 다릅니다.")
        if not base:
            raise RuntimeError("등록된 self base branch가 비어 있습니다.")
        return str(base)


    def fetch_bootstrap_revision(self, source: Path, state_dir: Path) -> tuple[str, str]:
        """Resolve the registered base and fetch that branch under the process lock."""

        source = _canonical_path(source)
        root = _canonical_path(Path(self.effects.git(source, "rev-parse", "--show-toplevel").stdout.strip()))
        if root != source:
            raise RuntimeError(f"Loop Manager source 저장소 루트가 아닙니다: {source}")
        origin = self.effects.github_repository(self.effects.git(source, "remote", "get-url", "origin").stdout)
        base = self.bootstrap_base_branch(source, state_dir, origin)
        sha = self.effects._recorded_base_fetch(source, state_dir, base, owner="bootstrap")
        return base, sha


    def validate_bootstrap_revision(self, source: Path, base: str, sha: str) -> None:
        """Reject a source or remote race before starting work at the selected SHA."""

        current = self.effects.git(source, "rev-parse", "HEAD").stdout.strip()
        branch_result = self.effects.git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
        remote = self.effects.git(
            source, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
        ).stdout.strip()
        if current != sha or remote != sha or branch != base:
            raise RuntimeError(
                f"bootstrap revision 변경: source={current} branch={branch} "
                f"origin/{base}={remote} expected={sha}"
            )
        operations = self.effects._git_operation_entries(source)
        if operations:
            raise RuntimeError("진행 중 Git operation: " + ", ".join(sorted(operations)))


    def prepare_bootstrap_runtime(self,
        source: Path, state_dir: Path, mutation_id: str,
    ) -> RuntimeWorktree | BootstrapRefreshHandoff:
        """Refresh an old source and hand control to the next external invocation."""

        base, sha = self.fetch_bootstrap_revision(source, state_dir)
        result = self.effects.synchronize_base_worktree(
            source, base, remote_sha=sha, state_dir=state_dir, owner="bootstrap",
        )
        if result.status == "updated":
            self.effects.operation_log(f"bootstrap source 갱신 완료: origin/{base} {sha}; 다음 실행에 인계")
            return BootstrapRefreshHandoff(base, sha)
        if result.status != "equal":
            raise RuntimeError(
                f"bootstrap source 갱신 불가 (origin/{base} {sha}): "
                f"{result.status}: {result.reason}"
            )
        self.validate_bootstrap_revision(source, base, sha)
        runtime = self.prepare_runtime_worktree(
            source, state_dir, mutation_id=mutation_id, commit_sha=sha,
        )
        self.validate_bootstrap_revision(source, base, sha)
        return runtime


    def run_runtime_child(self,
        source: Path,
        state_dir: Path,
        config_path: Path,
        runtime: RuntimeWorktree,
        arguments: list[str],
    ) -> int:
        child_arguments = runtime_child_arguments(
            arguments,
            source=source,
            state_dir=state_dir,
            config_path=config_path,
            runtime=runtime,
        )
        completed = subprocess.run(
            [
                self.interpreter,
                "-B",  # Keep immutable runtime imports from creating ignored bytecode.
                "-u",
                str(runtime.path / "loop_manager.py"),
                *child_arguments,
            ],
            cwd=runtime.path,
            check=False,
        )
        if runtime.mutation_id is not None:
            try:
                self.remove_runtime_worktree(source, state_dir, runtime)
            except (RuntimeError, OSError) as error:
                self.effects.operation_log(f"runtime 정리 보류: {error}", error=True)
        return completed.returncode


    def validate_runtime_child(self,
        source: Path,
        state_dir: Path,
        commit_sha: str,
    ) -> None:
        self.validate_runtime_worktree(source, state_dir, self.runtime_root, commit_sha)


    def validate_runtime_mutation(self,
        source: Path, state_dir: Path, sha: str, mutation_id: str | None,
    ) -> RuntimeMutation:
        """Bind a child invocation to the held parent lock and actual Git runtime."""
        runtime = expected_runtime_worktree(state_dir, source, sha)
        if _canonical_path(self.runtime_root) != _canonical_path(runtime):
            raise RuntimeError(f"runtime 경로가 선택한 SHA와 다릅니다: {self.runtime_root} (expected {runtime})")
        identity = _held_process_lock_identity(state_dir)
        if mutation_id is None:
            if identity != "0":
                raise RuntimeError("mutation ID가 없는 새 형식 runtime 호출은 허용되지 않습니다.")
            legacy = True
        else:
            if identity != f"runtime:{mutation_id}":
                raise RuntimeError("runtime mutation ID와 부모 process lock이 다릅니다.")
            legacy = False
        origin = self.effects.repository_origin(source)
        base = self.bootstrap_base_branch(source, state_dir, origin)
        origin_sha = self.effects.git(
            source, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
        ).stdout.strip()
        if origin_sha != sha:
            raise RuntimeError(
                f"runtime SHA가 현재 origin/{base}와 다릅니다: "
                f"{sha} (origin/{base} {origin_sha})"
            )
        self.validate_runtime_worktree(source, state_dir, runtime, sha)
        lock_reason = self._validated_runtime_registration_marker(
            source, state_dir, runtime, sha, mutation_id,
            legacy=legacy, record_legacy_adoption=True,
        )
        self.effects.operation_log(
            f"runtime mutation 검증: path={runtime} sha={sha} "
            f"mode={'legacy' if legacy else 'owned'}"
        )
        return RuntimeMutation(source, runtime, sha, mutation_id, legacy, lock_reason)
