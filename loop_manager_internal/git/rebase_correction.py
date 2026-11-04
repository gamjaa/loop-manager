"""Deterministic identity and next action for an owned rebase conflict."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import AbstractSet, Mapping


class CorrectionSource(str, Enum):
    REBASE = "rebase"
    AUTOSTASH = "autostash"


class CorrectionAction(str, Enum):
    WORKER = "worker"
    STAGE = "stage"
    CONTINUE = "continue"
    SKIP = "skip"
    VERIFY = "verify"
    USER_ACTION = "user_action"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def parse_unmerged(raw: str) -> tuple[tuple[str, int, str], ...]:
    stages = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        _mode, oid, stage = metadata.split(" ")
        stages.append((path, int(stage), oid))
    return tuple(sorted(stages))


def stage_evidence_preserved(
    accepted: Mapping[str, object] | None,
    observed: Mapping[str, object],
    tracked_paths: AbstractSet[str],
    clean_signatures: Mapping[str, str],
) -> bool:
    """Clean tracked paths must still contain the accepted resolution."""
    if not isinstance(accepted, Mapping) or not isinstance(observed, Mapping):
        return False
    if accepted == observed:
        return True
    if (accepted.get('head') != observed.get('head')
            or accepted.get('branch') != observed.get('branch')):
        return False
    accepted_files = accepted.get('files')
    observed_files = observed.get('files')
    if not isinstance(accepted_files, dict) or not isinstance(observed_files, dict):
        return False
    if any(path not in accepted_files or accepted_files[path] != signature
           for path, signature in observed_files.items()):
        return False
    disappeared = accepted_files.keys() - observed_files.keys()
    return (bool(disappeared) and disappeared <= tracked_paths
            and all(path in clean_signatures
                    and clean_signatures[path] == accepted_files[path]
                    for path in disappeared))


@dataclass(frozen=True)
class ConflictObservation:
    plan_revision: str | None
    target_base: str
    before_head: str
    stages: tuple[tuple[str, int, str], ...]
    worktree_files: tuple[tuple[str, str], ...]
    staged_diff: str
    rebase_position: str | None
    source: CorrectionSource
    stash_oid: str | None = None

    @property
    def fingerprint(self) -> str:
        return _digest((self.plan_revision, self.target_base, self.before_head,
                        self.stages))

    @property
    def progress(self) -> str:
        return _digest((self.stages, self.worktree_files, self.staged_diff,
                        self.rebase_position, self.source.value, self.stash_oid))

    @property
    def conflict_paths(self) -> tuple[str, ...]:
        return tuple(sorted({path for path, _, _ in self.stages}))


def conflict_wait_fingerprint(observation, row, *, checkpoint, service, git_bytes):
    """Bind a paused batch to its inputs and available recovery resources."""
    return _digest((observation.fingerprint, row['active_sha'], row['pr_url'],
                    row['pr_head_oid'], row['pr_conflict_head_oid'], row['pr_conflict_base_oid'],
                    checkpoint is not None, service is not None, git_bytes is not None))


def next_action(observation: ConflictObservation, *, worker_result: bool,
                staged: bool, safe: bool = True) -> CorrectionAction:
    if not safe or (observation.source is CorrectionSource.AUTOSTASH and not observation.stash_oid):
        return CorrectionAction.USER_ACTION
    if observation.stages:
        return CorrectionAction.STAGE if worker_result else CorrectionAction.WORKER
    if observation.source is CorrectionSource.AUTOSTASH:
        return CorrectionAction.VERIFY
    if not worker_result:
        return CorrectionAction.VERIFY
    return CorrectionAction.CONTINUE if staged else CorrectionAction.SKIP


@dataclass(frozen=True)
class GitStepResult:
    action: CorrectionAction
    returncode: int
    output: str


def execute_git_step(action: CorrectionAction, *, git: object, worktree: object,
                     paths: tuple[str, ...] = ()) -> GitStepResult:
    """Execute one typed Git step after the caller records its checkpoint."""
    if action is CorrectionAction.STAGE:
        if not paths:
            return GitStepResult(action, 0, "")
        arguments = ('add', '-A', '--', *paths)
    elif action is CorrectionAction.CONTINUE:
        arguments = ('-c', 'core.editor=true', 'rebase', '--continue')
    elif action is CorrectionAction.SKIP:
        arguments = ('rebase', '--skip')
    elif action is CorrectionAction.VERIFY:
        return GitStepResult(action, 0, "")
    else:
        raise ValueError(f"Git step is not executable: {action.value}")
    result = git(worktree, *arguments, check=False)
    return GitStepResult(action, result.returncode, result.stdout)


def observe_git(*, plan_revision: str | None, target_base: str,
                before_head: str, source: CorrectionSource, stash_oid: str | None,
                worktree: object, git: object, capture_snapshot: object) -> ConflictObservation:
    """Read only Git evidence. The caller has already established workspace ownership."""
    stages = parse_unmerged(git(worktree, "ls-files", "-u", "-z").stdout)
    staged = git(worktree, "diff", "--cached", "--raw", "--no-abbrev", "-z").stdout
    position = None
    if source is CorrectionSource.REBASE:
        head = git(worktree, "rev-parse", "--verify", "REBASE_HEAD", check=False)
        if head.returncode == 0:
            position = head.stdout.strip() or None
    snapshot = capture_snapshot(worktree)
    files = tuple(sorted((path, str(signature))
                         for path, (_flags, signature) in snapshot.dirty.items()))
    return ConflictObservation(plan_revision, target_base, before_head, stages,
                               files, staged, position, source, stash_oid)
