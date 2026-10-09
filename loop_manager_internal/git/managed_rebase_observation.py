"""Read-only adapter for the current-state managed rebase policy.

Callers supply existing workspace/role/approval boundaries and live PR queries.
No journal, turn, stage approval, reflog or original-commit reader is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Callable

from loop_manager_internal.agents.scope_observation import RAW_DIFF_OPTIONS
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.git.managed_rebase_validation import (
    FileChange, IndexEntry, ObservationFailure, RebaseExpectation,
    RebaseObservation, Remote, ScopeResult, WorktreeFile, assess_scope, fingerprint,
    _safe_path,
)


def parse_raw_changes(raw: str) -> tuple[FileChange, ...]:
    """Parse --raw --no-renames -z, retaining both modes and blob identities."""
    if not raw:
        return ()
    records = raw.split('\0')
    if records.pop() != '' or len(records) % 2:
        raise ValueError('incomplete raw diff')
    changes = []
    for offset in range(0, len(records), 2):
        fields = records[offset].split()
        if (len(fields) != 5 or not fields[0].startswith(':')
                or len(fields[4]) != 1 or not records[offset + 1]):
            raise ValueError('ambiguous raw diff')
        changes.append(FileChange(records[offset + 1], fields[0][1:], *fields[1:]))
    if len({change.path for change in changes}) != len(changes):
        raise ValueError('duplicate raw diff path')
    return tuple(sorted(changes, key=lambda change: change.path))


_DEBUG = re.compile(
    r'\0  ctime: [^\n]*\n  mtime: [^\n]*\n  dev: [^\n]*\n'
    r'  uid: [^\n]*\n  size: [^\n]*\tflags: ([0-9a-fA-F]+)\n'
)


def parse_index(index: dict, debug: str) -> tuple[IndexEntry, ...]:
    """Pair every index stage with Git's actual flags, not only ls-files -v."""
    flags: dict[str, list[int]] = {}
    position = 0
    for match in _DEBUG.finditer(debug):
        path = debug[position:match.start()]
        if not path:
            raise ValueError('missing debug index path')
        flags.setdefault(path, []).append(int(match.group(1), 16))
        position = match.end()
    if position != len(debug) or set(flags) != set(index):
        raise ValueError('incomplete debug index flags')
    entries = []
    for path, metadata in sorted(index.items()):
        if len(flags[path]) != len(metadata):
            raise ValueError('index stage/flag count changed')
        remaining = list(flags[path])
        for record in metadata:
            tag, mode, oid, stage_text = record.split()
            stage = int(stage_text)
            matching = [flag for flag in remaining if (flag & 0x3000) == stage << 12]
            if len(matching) != 1:
                raise ValueError('index stage/flag identity ambiguous')
            flag = matching[0]
            remaining.remove(flag)
            entries.append(IndexEntry(path, mode, oid, stage, tag, flag))
    return tuple(entries)


@dataclass(frozen=True)
class ObservationAdapters:
    git: Callable
    capture_snapshot: Callable
    # Reuse validate_execplan_workspace_identity and validate_managed_worktree,
    # with owned detached rebases permitted. Raise on any identity uncertainty.
    validate_identity: Callable[[RebaseExpectation], None]
    read_rebase_metadata: Callable
    # Must query the live origin ref and PR identity/head, including absence.
    read_remote: Callable[[RebaseExpectation], Remote]
    # Recompute from current plan/source/approval inputs, not saved Worker turns.
    read_inputs: Callable[[RebaseExpectation], tuple[str, str]]
    # Return the actual Git mode and full file signature; pass this observation's
    # first index mode for Windows, which has no POSIX executable-bit semantics.
    read_file: Callable[[Path, str, str | None], tuple[str, str]]
    path_allowed: Callable[[str], bool]
    contracts_allowed: Callable[[RebaseObservation], bool]
    normalize_ignored_snapshot: Callable | None = None


def _stdout(adapters: ObservationAdapters, worktree: Path, *args: str) -> str:
    result = adapters.git(worktree, *args, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'Git observation failed: {args[0]}')
    return result.stdout


def _once(expected: RebaseExpectation, adapters: ObservationAdapters, *, start=None):
    repo, worktree = Path(expected.repo), Path(expected.worktree)
    if str(repo.resolve()) != expected.repo or str(worktree.resolve()) != expected.worktree:
        raise ValueError('noncanonical repository or workspace')
    adapters.validate_identity(expected)
    identities = []
    for context in (repo, worktree):
        raw = _stdout(adapters, context, 'rev-parse', '--path-format=absolute',
                      '--git-common-dir').strip()
        if not raw:
            raise ValueError('repository identity missing')
        path = Path(raw)
        identities.append(str((path if path.is_absolute() else context / path).resolve()))
    if identities[0] != identities[1]:
        raise ValueError('repository identity mismatch')
    target = _stdout(adapters, worktree, 'rev-parse', '--verify',
                     f'{expected.target_base_oid}^{{commit}}').strip()
    if target != expected.target_base_oid:
        raise ValueError('target base is not an immutable commit identity')
    details = {}
    snapshot = adapters.capture_snapshot(worktree, details=details)
    if not snapshot.shared_metadata_complete:
        raise ValueError('incomplete repository observation')
    metadata = adapters.read_rebase_metadata(
        worktree, **({'git_paths': details['git_paths']} if 'git_paths' in details else {}))
    inputs, scope_policy = adapters.read_inputs(expected)
    remote = adapters.read_remote(expected)
    if not isinstance(remote, Remote):
        raise ValueError('remote observation missing')
    ancestor = adapters.git(worktree, 'merge-base', '--is-ancestor',
                            expected.target_base_oid, snapshot.head, check=False)
    if ancestor.returncode not in {0, 1}:
        raise RuntimeError('조상 commit 관측에 실패했습니다.')
    debug = details['index_debug'] if 'index_debug' in details else _stdout(
        adapters, worktree, 'ls-files', '--debug', '-z')
    index = parse_index(snapshot.index, debug)
    diff_options = RAW_DIFF_OPTIONS
    diff_base = expected.target_base_oid
    if (ancestor.returncode == 1 and snapshot.head == expected.before_head
            and metadata is None and not controlling_git_operation(snapshot.operations)):
        diff_base = _stdout(adapters, worktree, 'merge-base',
                            expected.target_base_oid, snapshot.head).strip()
        if not diff_base:
            raise ValueError('original branch has no common target base')
    committed = parse_raw_changes(_stdout(
        adapters, worktree, 'diff', *diff_options,
        diff_base, snapshot.head, '--',
    ))
    staged = parse_raw_changes(details['staged_raw'] if 'staged_raw' in details else _stdout(
        adapters, worktree, 'diff', '--cached', *diff_options, '--'))
    kinds = {path: set(value[0]) for path, value in snapshot.dirty.items()}
    if 'ignored_paths' in details:
        ignored_paths = details['ignored_paths']
    else:
        ignored = _stdout(adapters, worktree, 'ls-files', '--others', '--ignored',
                          '--exclude-standard', '-z')
        if ignored and not ignored.endswith('\0'):
            raise ValueError('incomplete ignored file list')
        ignored_paths = tuple(filter(None, ignored.split('\0')))
    for path in ignored_paths:
        kinds.setdefault(path, set()).add('ignored')
    # core.filemode=false can hide a tracked mode change from Git diff. Inspect
    # actual modes as well; the file reader handles platform Git mode semantics.
    tracked_modes = {entry.path: entry.mode for entry in index if entry.stage == 0}
    index_modes = {}
    for entry in index:
        index_modes.setdefault(entry.path, entry.mode)
    files = []
    for path in sorted(kinds.keys() | tracked_modes.keys()):
        # Never follow a malformed Git path outside the selected worktree.
        if not _safe_path(path):
            raise ValueError('unsafe working file path')
        mode, signature = adapters.read_file(worktree, path, index_modes.get(path))
        path_kinds = set(kinds.get(path, ()))
        if path in tracked_modes and mode != tracked_modes[path]:
            path_kinds.add('unstaged')
        if path in snapshot.dirty and signature != snapshot.dirty[path][1]:
            raise ValueError('working file changed during observation')
        if path_kinds:
            files.append(WorktreeFile(path, tuple(sorted(path_kinds)), mode, signature))
    if adapters.normalize_ignored_snapshot is not None:
        ignored_files = {file.path: (("untracked",), file.signature) for file in files
                         if file.kinds == ("ignored",) and file.path not in snapshot.index}
        if ignored_files:
            candidate = replace(snapshot, dirty={**snapshot.dirty, **ignored_files})
            normalized = adapters.normalize_ignored_snapshot(worktree, candidate, snapshot, start=start or snapshot)
            excluded = ignored_files.keys() - normalized.dirty.keys()
            files = [file for file in files if file.path not in excluded]
    registration = snapshot.worktrees.get(expected.worktree)
    observed = RebaseObservation(
        repo=expected.repo, repository_identity=identities[0], worktree=expected.worktree,
        identity_verified=True, head=snapshot.head, branch=snapshot.branch,
        branch_ref=snapshot.refs.get(f'refs/heads/{expected.branch}'),
        registration=tuple(registration) if registration is not None else None,
        branch_worktrees=tuple(sorted(
            path for path, fields in snapshot.worktrees.items()
            if f'branch=refs/heads/{expected.branch}' in fields
        )),
        operations=tuple(sorted(snapshot.operations.items())),
        metadata=tuple(sorted(metadata.items())) if metadata is not None else None,
        base_is_ancestor=ancestor.returncode == 0,
        index_clean=not staged, index=index,
        committed=committed, staged=staged, files=tuple(files), remote=remote,
        input_fingerprint=inputs, scope_fingerprint=scope_policy,
        repository_state_fingerprint=fingerprint({
            'refs': snapshot.refs, 'worktrees': snapshot.worktrees, 'config': snapshot.config,
        }),
    )
    return observed, snapshot


def observe_current_rebase(
    expected: RebaseExpectation, adapters: ObservationAdapters,
) -> tuple[RebaseObservation | ObservationFailure, ScopeResult | None]:
    """Require stable observations; query failures cannot masquerade as absence."""
    try:
        first, start = _once(expected, adapters)
        observed, _snapshot = _once(expected, adapters, start=start)
        if first != observed:
            return ObservationFailure('state_changed', 'Git, remote or inputs changed'), None
        scope = assess_scope(
            expected, observed, path_allowed=adapters.path_allowed,
            contracts_allowed=adapters.contracts_allowed,
        )
        return observed, scope
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        return ObservationFailure('query_or_scope_failed', str(error)), None
