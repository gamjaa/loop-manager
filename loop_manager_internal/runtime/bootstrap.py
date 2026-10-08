"""Bootstrap and exact-revision runtime lifecycle with explicit Git effects.

Git mutation and journal policy remain in the existing Git adapters. This
service owns runtime selection, ownership validation and child handoff only.
"""
from __future__ import annotations
from contextlib import closing

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from loop_manager_internal.git import manager_git_journal
from .contracts import RuntimeMutation, RuntimeWorktree, WorkspaceOwnershipError
from .process_lock import _held_process_lock_identity
from .runtime_identity import (
    _canonical_path, _read_runtime_intent, _record_legacy_runtime_adoption,
    _validate_legacy_runtime_adoption, _write_runtime_intent,
    expected_runtime_worktree, runtime_child_arguments, runtime_repository_id,
)


_RUNTIME_BYTECODE_PREPARE = r'''
import importlib.util, json, marshal, pathlib, py_compile, sys, types

counts = {"created": 0, "reused": 0, "replaced": 0, "unavailable": 0, "missing": 0}
request = json.load(sys.stdin)

def valid_cache(data, source_bytes):
    try:
        return (
            len(data) >= 16
            and data[:4] == importlib.util.MAGIC_NUMBER
            and int.from_bytes(data[4:8], "little") == 3
            and data[8:16] == importlib.util.source_hash(source_bytes)
            and isinstance(marshal.loads(data[16:]), types.CodeType)
        )
    except (EOFError, TypeError, ValueError, OverflowError, RecursionError):
        return False

for name in request["sources"]:
    source = pathlib.Path(name)
    try:
        if source.is_symlink() or not source.is_file():
            counts["unavailable"] += 1
            continue
        source_bytes = source.read_bytes()
        cache = pathlib.Path(importlib.util.cache_from_source(str(source.absolute())))
        if cache.is_symlink():
            counts["unavailable"] += 1
            continue
        try:
            data = cache.read_bytes()
        except FileNotFoundError:
            data = None
        except OSError:
            counts["unavailable"] += 1
            continue
        if data is not None and valid_cache(data, source_bytes):
            counts["reused"] += 1
            continue
        existed = data is not None
        if not request["write_enabled"]:
            counts["unavailable" if existed else "missing"] += 1
            continue
        py_compile.compile(
            str(source.absolute()), str(cache), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        if not valid_cache(cache.read_bytes(), source_bytes):
            counts["unavailable"] += 1
            continue
        counts["replaced" if existed else "created"] += 1
    except (OSError, ValueError, py_compile.PyCompileError):
        counts["unavailable"] += 1
print(json.dumps(counts))
'''
_DEFAULT_BYTECODE_PREFIX_MARKER = "LOOP_MANAGER_RUNTIME_DEFAULT_PYCACHEPREFIX"


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


    def _runtime_bytecode_cache_prefix(
        self, source: Path, state_dir: Path, runtime: RuntimeWorktree,
    ) -> Path:
        """Keep standard Python caches outside disposable exact-revision worktrees."""
        interpreter = Path(self.interpreter).expanduser().resolve()
        try:
            metadata = interpreter.stat()
            identity = "\0".join((
                str(interpreter), str(metadata.st_dev), str(metadata.st_ino),
                str(metadata.st_size), str(metadata.st_mtime_ns),
            ))
        except OSError:
            identity = str(interpreter)
        interpreter_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        cache_tag = sys.implementation.cache_tag or "untagged"
        return (_canonical_path(state_dir) / "runtime-bytecode" / runtime_repository_id(source)
                / runtime.commit_sha / f"{cache_tag}-{interpreter_key}")


    def _runtime_child_environment(
        self,
    ) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop(_DEFAULT_BYTECODE_PREFIX_MARKER, None)
        if sys.dont_write_bytecode:
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if sys.pycache_prefix is not None:
            environment["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix
        if (sys.pycache_prefix is None and not any(
            key.casefold() == "pythonpycacheprefix" and value
            for key, value in environment.items()
        )):
            environment[_DEFAULT_BYTECODE_PREFIX_MARKER] = "1"
        return environment


    def _runtime_cache_prefix(
        self, source: Path, state_dir: Path, runtime: RuntimeWorktree,
    ) -> str:
        if sys.pycache_prefix is not None:
            return sys.pycache_prefix
        selected = next((value for key, value in os.environ.items()
                         if key.casefold() == "pythonpycacheprefix" and value), None)
        return selected or str(self._runtime_bytecode_cache_prefix(source, state_dir, runtime))


    def _prepare_runtime_bytecode_cache(
        self, runtime: RuntimeWorktree, environment: Mapping[str, str], cache_prefix: str,
    ) -> bool:
        write_enabled = not (sys.dont_write_bytecode or any(
            key.casefold() == "pythondontwritebytecode" and value
            for key, value in environment.items()
        ))
        started = time.perf_counter()
        try:
            result = self.effects.git(runtime.path, "ls-files", "-z", check=False)
            if result.returncode:
                raise RuntimeError("tracked runtime source 목록을 확인하지 못했습니다.")
            tracked = result.stdout
            if isinstance(tracked, bytes):
                tracked = os.fsdecode(tracked)
            sources = [path for path in tracked.split("\0") if path.endswith(".py")
                       and (path == "loop_manager.py" or path.startswith("loop_manager_internal/"))]
            if not sources:
                return False
            prepared = subprocess.run(
                [self.interpreter, "-X", f"pycache_prefix={cache_prefix}",
                 "-S", "-c", _RUNTIME_BYTECODE_PREPARE],
                input=json.dumps({"sources": sources, "write_enabled": write_enabled}),
                cwd=runtime.path, env=dict(environment),
                capture_output=True, text=True, check=False,
            )
            if prepared.returncode:
                raise RuntimeError("Python cache 사전 검증 프로세스가 실패했습니다.")
            counts = json.loads(prepared.stdout)
            self.effects.operation_log(
                "runtime Python bytecode cache: "
                f"checked-hash created={counts['created']} reused={counts['reused']} "
                f"replaced={counts['replaced']} unavailable={counts['unavailable']} "
                f"missing={counts['missing']} write_enabled={write_enabled} "
                f"seconds={time.perf_counter() - started:.3f}"
            )
            return counts["unavailable"] > 0
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            self.effects.operation_log(
                "runtime Python bytecode cache 사전 확인을 건너뜀: "
                f"{error} (seconds={time.perf_counter() - started:.3f})", error=True,
            )
            return True


    def validate_runtime_worktree(self,
        source: Path,
        state_dir: Path,
        runtime: Path,
        commit_sha: str,
        *, command_id: str | None = None,
    ) -> None:
        """Prove that an existing runtime is the clean detached worktree we own."""

        expected = expected_runtime_worktree(state_dir, source, commit_sha, command_id=command_id)
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
        commit_sha: str | None = None, concurrent: bool = False,
    ) -> RuntimeWorktree:
        """Select an immutable linked runtime worktree at a fetched commit."""

        source = _canonical_path(source)
        if commit_sha is None:
            _, commit_sha = self.fetch_bootstrap_revision(source, state_dir)
        runtime = expected_runtime_worktree(state_dir, source, commit_sha,
            command_id=mutation_id if concurrent else None)
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
                source, state_dir, RuntimeWorktree(runtime, commit_sha, mutation_id, concurrent),
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
            self.validate_runtime_worktree(source, state_dir, runtime, commit_sha,
                command_id=mutation_id if concurrent else None)
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
                if lock_reason == f"loop-manager-runtime:{mutation_id}":
                    return RuntimeWorktree(runtime, commit_sha, mutation_id, concurrent)
                _write_runtime_intent(
                    state_dir, mutation_id, source, runtime, commit_sha,
                    created=False, lock_reason=lock_reason if isinstance(lock_reason, str) else None,
                )
            return RuntimeWorktree(runtime, commit_sha, mutation_id, concurrent)

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
                self.validate_runtime_worktree(source, state_dir, runtime, commit_sha,
                    command_id=mutation_id if concurrent else None)

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
        self.validate_runtime_worktree(source, state_dir, runtime, commit_sha,
            command_id=mutation_id if concurrent else None)
        return RuntimeWorktree(runtime, commit_sha, mutation_id, concurrent)


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
            state_dir, path, expected_runtime_worktree(state_dir, source, sha,
                command_id=runtime.mutation_id if runtime.concurrent else None),
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
        self.validate_runtime_worktree(source, state_dir, path, sha,
            command_id=runtime.mutation_id if runtime.concurrent else None)
        before = self._runtime_transition_state(source, path)
        lock_reason = before["registration"]["locked"]
        self._validate_runtime_lock_origin(state_dir, source, path, sha, lock_reason)
        # Do not let Git remove ignored user data as part of an otherwise clean tree.
        if self.effects._ignored_worktree_paths(path):
            raise WorkspaceOwnershipError("runtime ignored 파일을 보존하기 위해 정리를 중단합니다.")
        receipts.mkdir(parents=True, exist_ok=True)
        manager_git_journal._sync_directory(state_dir)

        def apply(token: str) -> None:
            self.validate_runtime_worktree(source, state_dir, path, sha,
                command_id=runtime.mutation_id if runtime.concurrent else None)
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
        """Reject a remote race without making the user's checkout an execution gate."""
        remote = self.effects.git(
            source, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
        ).stdout.strip()
        if remote != sha:
            raise RuntimeError(
                f"bootstrap revision 변경: origin/{base}={remote} expected={sha}"
            )


    def prepare_bootstrap_runtime(self,
        source: Path, state_dir: Path, mutation_id: str,
        *, concurrent: bool = False,
    ) -> RuntimeWorktree:
        """Select remote code independently of best-effort source synchronization.

        The child enters this revision's main bootstrap under the parent's lease
        and validates its runtime identity before executing the requested command.
        """

        base, sha = self.fetch_bootstrap_revision(source, state_dir)
        if concurrent:
            runtime = self.prepare_runtime_worktree(
                source, state_dir, mutation_id=mutation_id, commit_sha=sha, concurrent=True,
            )
            self.validate_bootstrap_revision(source, base, sha)
            return runtime
        try:
            result = self.effects.synchronize_base_worktree(
                source, base, remote_sha=sha, state_dir=state_dir, owner="bootstrap",
            )
            self.effects.operation_log(
                f"bootstrap source 동기화 {result.status} (origin/{base} {sha}): {result.reason}",
                error=result.status in {"skipped", "failed"},
            )
        except (RuntimeError, OSError) as error:
            self.effects.operation_log(f"bootstrap source 동기화 보류 (origin/{base} {sha}): {error}", error=True)
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
        environment = self._runtime_child_environment()
        cache_prefix = self._runtime_cache_prefix(source, state_dir, runtime)
        fallback_cache = None
        if self._prepare_runtime_bytecode_cache(runtime, environment, cache_prefix):
            fallback_cache = tempfile.TemporaryDirectory(prefix="loop-manager-runtime-bytecode-")
            cache_prefix = fallback_cache.name
            if environment.get(_DEFAULT_BYTECODE_PREFIX_MARKER) != "1":
                environment["PYTHONPYCACHEPREFIX"] = cache_prefix
            self.effects.operation_log(
                "runtime Python bytecode cache: 검증되지 않은 cache를 피하기 위해 임시 prefix 사용"
            )
        try:
            completed = subprocess.run(
                [
                    self.interpreter,
                    *(["-B"] if sys.dont_write_bytecode else []),
                    "-X",
                    f"pycache_prefix={cache_prefix}",
                    "-u",
                    str(runtime.path / "loop_manager.py"),
                    *child_arguments,
                ],
                cwd=runtime.path,
                check=False,
                env=environment,
            )
        finally:
            if fallback_cache is not None:
                fallback_cache.cleanup()
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
        *, command_id: str | None = None,
    ) -> None:
        self.validate_runtime_worktree(source, state_dir, self.runtime_root, commit_sha,
            command_id=command_id)


    def _selected_runtime_revision(self, source, state_dir, base, sha, current):
        """Prove the selected fetch historically; a later base is separate input."""
        ref = f'refs/remotes/origin/{base}'
        try:
            remote = self.effects.git(source, 'ls-remote', '--heads', 'origin', f'refs/heads/{base}', check=False)
            if remote.returncode or remote.stdout.split() != [current, f'refs/heads/{base}']:
                return False
            if self.effects.git(source, 'merge-base', '--is-ancestor', sha, current, check=False).returncode:
                return False
            slot = manager_git_journal._slot(state_dir, str(_canonical_path(source)), 'bootstrap_fetch', ref)
            lines = self.effects.git(source, 'reflog', 'show', '--format=%H%x00%gs', ref, check=False)
            if lines.returncode:
                return False
            for path in slot.glob('*.result.json'):
                result = manager_git_journal._read(path)
                intent = manager_git_journal._read(slot / f"{result['id']}.intent.json")
                if (intent['id'] != result['id'] or intent['owner'] != 'bootstrap'
                        or intent['repo'] != str(_canonical_path(source))
                        or intent['kind'] != 'bootstrap_fetch' or intent['target'] != ref
                        or intent['expected'] != {'oid': sha} or result['status'] != 'complete'):
                    continue
                if (manager_git_journal.verified_transition(intent, result['observed'], True) == result['candidates']
                        and lines.stdout.splitlines().count(f"{sha}\0loop-manager-base-fetch:{intent['id']}") == 1):
                    live = self.effects.git(source, 'rev-parse', '--verify', ref, check=False)
                    return live.returncode == 0 and live.stdout.strip() == current
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            pass
        return False

    def validate_runtime_mutation(self,
        source: Path, state_dir: Path, sha: str, mutation_id: str | None,
        *, concurrent: bool = False,
    ) -> RuntimeMutation:
        """Bind a child invocation to the held parent lock and actual Git runtime."""
        if concurrent and mutation_id is None:
            raise RuntimeError("병행 runtime에는 고유 mutation ID가 필요합니다.")
        runtime = expected_runtime_worktree(state_dir, source, sha,
            command_id=mutation_id if concurrent else None)
        if _canonical_path(self.runtime_root) != _canonical_path(runtime):
            raise RuntimeError(f"runtime 경로가 선택한 SHA와 다릅니다: {self.runtime_root} (expected {runtime})")
        identity = _held_process_lock_identity(state_dir,
            name=f"manual-{mutation_id}.lock" if concurrent else "loop-manager.lock")
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
        if origin_sha != sha and not self._selected_runtime_revision(source, state_dir, base, sha, origin_sha):
            raise RuntimeError(
                f"runtime SHA가 현재 origin/{base}와 다릅니다: "
                f"{sha} (origin/{base} {origin_sha})"
            )
        self.validate_runtime_worktree(source, state_dir, runtime, sha,
            command_id=mutation_id if concurrent else None)
        lock_reason = self._validated_runtime_registration_marker(
            source, state_dir, runtime, sha, mutation_id,
            legacy=legacy, record_legacy_adoption=True,
        )
        self.effects.operation_log(
            f"runtime mutation 검증: path={runtime} sha={sha} "
            f"mode={'legacy' if legacy else 'owned'}"
        )
        return RuntimeMutation(source, runtime, sha, mutation_id, legacy, lock_reason, concurrent)
