"""Phase-bound, content-free proof of a Manager-owned rebase replay.

Git observation belongs to callers.  The verdict below only compares frozen
source and replay facts, so a later commit may change a resolved path without
changing the proof of the earlier resolution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
from typing import Any


EVIDENCE_VERSION = 2


class ReplayEvidenceError(RuntimeError):
    def __init__(self, phase: str, source: str | None, replayed: str | None,
                 reason: str):
        self.phase = phase
        self.source = source
        self.replayed = replayed
        self.reason = reason
        super().__init__(
            f"rebase {phase}: {reason}; source={source or '-'}; "
            f"replayed={replayed or '-'}"
        )


def source_commits(git: Callable[..., Any], repo: Any, original: str,
                   target: str) -> tuple[str, list[str]]:
    """Freeze the exact commit sequence used by Git's default linear rebase."""
    base = git(repo, 'merge-base', original, target).stdout.strip()
    if not base:
        raise ReplayEvidenceError('start', None, None, 'merge base unavailable')
    result = git(repo, 'rev-list', '--reverse', f'{base}..{original}')
    commits = result.stdout.splitlines()
    if result.returncode or any(not commit for commit in commits):
        raise ReplayEvidenceError('start', None, None, 'source order unavailable')
    return base, commits


def replay_commits(git: Callable[..., Any], repo: Any, target: str,
                   head: str) -> list[str]:
    if target == head:
        return []
    result = git(repo, 'rev-list', '--reverse', f'{target}..{head}')
    if result.returncode:
        raise ReplayEvidenceError('replay', None, None, 'replay order unavailable')
    return result.stdout.splitlines()


def commit_fact(git: Callable[..., Any], repo: Any, oid: str) -> dict[str, str]:
    result = git(repo, 'rev-list', '--parents', '-n', '1', oid)
    words = result.stdout.strip().split()
    if result.returncode or len(words) != 2 or words[0] != oid:
        raise ReplayEvidenceError('replay', None, oid, 'commit parent unavailable')
    tree = git(repo, 'rev-parse', f'{oid}^{{tree}}').stdout.strip()
    if not tree:
        raise ReplayEvidenceError('replay', None, oid, 'commit tree unavailable')
    return {'oid': oid, 'parent': words[1], 'tree': tree}


def patch_equal(git: Callable[..., Any], repo: Any, source: Mapping[str, str],
                replayed: Mapping[str, str]) -> bool:
    """Use range-diff only for one ordinary commit, never across resolutions."""
    result = git(
        repo, 'range-diff', '--no-color', '--no-patch',
        f"{source['parent']}..{source['oid']}",
        f"{replayed['parent']}..{replayed['oid']}", check=False,
    )
    lines = result.stdout.splitlines()
    return result.returncode == 0 and len(lines) == 1 and bool(
        re.match(r'^\s*1:\s+[0-9a-f]+\s+=\s+1:\s+[0-9a-f]+\s+', lines[0])
    )


def stage_matches(*, before_index: Mapping[str, list[str]],
                  staged_index: Mapping[str, list[str]],
                  allowed_paths: Sequence[str], accepted_files: Mapping[str, str],
                  staged_dirty: Mapping[str, Any]) -> bool:
    """Reject changes outside the correction and non-default stage/index flags."""
    if not isinstance(before_index, Mapping) or not isinstance(staged_index, Mapping):
        return False
    allowed = set(allowed_paths)
    if not allowed or not set(accepted_files) <= allowed:
        return False
    for path in before_index.keys() | staged_index.keys():
        if before_index.get(path) != staged_index.get(path):
            if path not in allowed:
                return False
            entries = staged_index.get(path, [])
            if (not isinstance(entries, list) or any(
                    not re.fullmatch(r'H (?:100644|100755|120000|160000) [0-9a-f]{40,64} 0', entry)
                    for entry in entries)):
                return False
    return all(
        isinstance(staged_dirty.get(path), list)
        and len(staged_dirty[path]) == 2
        and staged_dirty[path][1] == signature
        for path, signature in accepted_files.items()
    )


def verify_replay(*, sources: Sequence[str], replayed: Sequence[Mapping[str, str]],
                  target: str, head: str,
                  corrections: Mapping[str, Mapping[str, str]],
                  skips: Mapping[str, Mapping[str, str]],
                  source_facts: Mapping[str, Mapping[str, str]],
                  same_patch: Callable[[Mapping[str, str], Mapping[str, str]], bool],
                  allow_prefix: bool = False,
                  stop_source: str | None = None) -> list[dict[str, str]]:
    """Require a unique ordered source-to-result mapping and contiguous parents."""
    if len(set(sources)) != len(sources) or set(corrections) & set(skips):
        raise ReplayEvidenceError('start', None, None, 'duplicate source evidence')
    if (set(corrections) | set(skips)) - set(sources):
        raise ReplayEvidenceError('start', None, None, 'foreign correction source')
    cursor = target
    position = 0
    mapping: list[dict[str, str]] = []
    stopped = False
    for source in sources:
        if allow_prefix and position == len(replayed):
            if stop_source is None or source == stop_source:
                stopped = stop_source is not None
                break
        skipped = skips.get(source)
        if skipped is not None:
            if (skipped.get('source') != source
                    or skipped.get('parent') != cursor
                    or skipped.get('reason') not in {'empty', 'worker-approved'}
                    or not skipped.get('intent_id')):
                raise ReplayEvidenceError('skip', source, None, 'skip receipt mismatch')
            mapping.append({'source': source, 'outcome': 'skip', 'parent': cursor,
                            'intent_id': skipped['intent_id']})
            continue
        if position >= len(replayed):
            raise ReplayEvidenceError('replay', source, None, 'source commit missing')
        commit = replayed[position]
        oid = commit.get('oid')
        if commit.get('parent') != cursor or not oid or not commit.get('tree'):
            raise ReplayEvidenceError('replay', source, oid, 'parent or tree mismatch')
        correction = corrections.get(source)
        if correction is not None:
            if (correction.get('source') != source
                    or correction.get('parent') != cursor
                    or correction.get('tree') != commit['tree']
                    or not correction.get('intent_id')
                    or not correction.get('turn_id')):
                raise ReplayEvidenceError('resolution', source, oid,
                                          'approved parent, tree or turn mismatch')
            outcome = 'resolved'
        elif not same_patch(source_facts[source], commit):
            raise ReplayEvidenceError('patch', source, oid, 'ordinary patch changed')
        else:
            outcome = 'patch'
        mapped = {'source': source, 'replayed': oid, 'outcome': outcome,
                  'parent': cursor, 'tree': commit['tree']}
        if correction is not None:
            mapped['intent_id'] = correction['intent_id']
            mapped['turn_id'] = correction['turn_id']
        mapping.append(mapped)
        cursor = oid
        position += 1
    if position != len(replayed) or cursor != head:
        raise ReplayEvidenceError('replay', None, head, 'extra commit or final HEAD mismatch')
    if allow_prefix and stop_source is not None and not stopped:
        raise ReplayEvidenceError('replay', stop_source, head,
                                  'current conflict source is not next')
    if not allow_prefix and len(mapping) != len(sources):
        raise ReplayEvidenceError('replay', None, head, 'source sequence incomplete')
    return mapping
