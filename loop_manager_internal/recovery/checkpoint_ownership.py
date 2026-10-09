"""Proof of a published plan checkpoint's last accepted remote head."""

from __future__ import annotations

import sqlite3
import re
import json
from pathlib import Path
from typing import Callable
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason


def publication_receipt(row, remote_oid):
    """현재 요청을 바꾸지 않고 같은 소유 작업의 과거 게시 영수증을 읽는다."""
    if row is None or row['phase'] == 'published':
        return dict(row) if row is not None else None
    try:
        history = json.loads(row['publication_history'])
        if not isinstance(history, list):
            raise ValueError('publication history must be a list')
        for entry in reversed(history):
            if not isinstance(entry, dict):
                raise ValueError('publication history entry must be an object')
            prior = entry.get('superseded_request', entry)
            if not isinstance(prior, dict):
                raise ValueError('publication receipt must be an object')
            # 구형 flat 영수증은 현재 요청의 정확한 lease와 소유 공간에 결합한다.
            legacy = 'superseded_request' not in entry and 'branch' not in prior
            if legacy and row['expected_remote_oid'] != remote_oid:
                continue
            receipt = {**dict(row), **prior}
            if (receipt['repo'] != row['repo'] or receipt['plan_id'] != row['plan_id']
                    or receipt['branch'] != row['branch'] or receipt['worktree'] != row['worktree']
                    or receipt['pr_url'] != row['pr_url']
                    or receipt['accepted_remote_oid'] != remote_oid
                    or not receipt['published_oid'] or not receipt['commit_identity']
                    or not receipt['tree_oid'] or receipt['pending_remote_oid']
                    or (not legacy and (receipt['phase'] != 'published'
                                        or receipt['result'] != 'published'))):
                continue
            return {**receipt, 'phase': 'published', 'result': 'published',
                    'request_identity': row['request_identity']}
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                              'checkpoint publication history observation failed') from error
    return None


def owned_remote_oid(
    connection: sqlite3.Connection, repo_path: Path, repo: str,
    plan_id: str, branch: str, worktree: Path, remote_oid: str | None,
    git: Callable[..., object], *, before_head: str | None = None,
    recorded_oid: str | None = None,
    require_live: bool = True,
    strict_observation: bool = False,
    allow_history: bool = False,
) -> str | None:
    """Return an OID only when durable identity and live origin both prove it."""
    if not remote_oid:
        return None
    try:
        row = connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if strict_observation:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint ownership database observation failed') from error
        raise
    if allow_history:
        row = publication_receipt(row, remote_oid)
    if row is None or any((
        row['phase'] != 'published', row['result'] != 'published',
        row['branch'] != branch, row['worktree'] != str(worktree),
        row['accepted_remote_oid'] != remote_oid,
        bool(row['pending_remote_oid']),
        not row['published_oid'], not row['commit_identity'], not row['tree_oid'],
        recorded_oid is not None and recorded_oid != remote_oid,
    )):
        return None
    published = str(row['published_oid'])
    try:
        message = git(repo_path, 'show', '-s', '--format=%B', published).stdout
        tree = git(repo_path, 'rev-parse', f'{published}^{{tree}}').stdout.strip()
        if tree != row['tree_oid']:
            return None
        if f"Checkpoint-Identity: {row['commit_identity']}" in message.splitlines():
            if (message.splitlines()[0] != f'[{plan_id}] BLOCKED checkpoint'
                    or 'Scope: plan' not in message.splitlines()):
                return None
        else:
            # Older checkpoint rows only recorded the current block inputs.
            keys = row.keys()
            identity_reason = (
                row['identity_reason_code']
                if 'identity_reason_code' in keys else None
            )
            identity_evidence = (
                row['identity_evidence']
                if 'identity_evidence' in keys else None
            )
            if (
                row['local_oid'] != published
                or row['commit_identity'] != blocked_checkpoint.checkpoint_identity(
                    plan_id=plan_id,
                    reason_code=str(identity_reason if identity_reason is not None
                                    else row['reason_code']),
                    evidence=str(identity_evidence if identity_evidence is not None
                                 else row['evidence']),
                    parent_oid=published, tree_oid=tree, included_paths=(),
                )
            ):
                return None
        ancestry = git(repo_path, 'merge-base', '--is-ancestor', published,
                       remote_oid, check=False)
        if ancestry.returncode not in (0, 1):
            raise RuntimeError('checkpoint ancestry observation failed')
        if ancestry.returncode == 1:
            return None
        if before_head:
            ancestry = git(repo_path, 'merge-base', '--is-ancestor',
                           remote_oid, before_head, check=False)
            if ancestry.returncode not in (0, 1):
                raise RuntimeError('checkpoint head ancestry observation failed')
            if ancestry.returncode == 1:
                return None
        if not require_live:
            return remote_oid
        observed = git(repo_path, 'ls-remote', '--heads', 'origin',
                       f'refs/heads/{branch}', check=False)
    except (OSError, RuntimeError) as error:
        if strict_observation:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint ownership Git observation failed') from error
        return None
    if observed.returncode:
        if strict_observation:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint live remote observation failed')
        return None
    lines = [line.split() for line in observed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if (len(lines) != 1 or len(lines[0]) != 2
            or lines[0][1] != f'refs/heads/{branch}'
            or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', lines[0][0]) is None):
        if strict_observation:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint live remote response malformed')
        return None
    if lines[0][0] != remote_oid:
        return None
    return remote_oid


def committed_tree_matches_intent(
    repo_path: Path, before_head: str | None, head: str,
    snapshot: dict, git: Callable[..., object], *, strict_observation: bool = False,
) -> bool:
    """Match an interrupted Manager commit to its approved staged tree."""
    tree = snapshot.get('commit_tree_oid')
    if (not before_head or before_head == head
            or snapshot.get('before_head') != before_head
            or not snapshot.get('commit_message')
            or not isinstance(tree, str)
            or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', tree) is None):
        return False
    try:
        return (
            git(repo_path, 'show', '-s', '--format=%P', head).stdout.strip()
            == before_head
            and git(repo_path, 'show', '-s', '--format=%B', head).stdout.strip()
            == str(snapshot['commit_message']).strip()
            and git(repo_path, 'rev-parse', f'{head}^{{tree}}').stdout.strip()
            == tree
        )
    except (OSError, RuntimeError) as error:
        if strict_observation:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint commit observation failed') from error
        return False


def rebase_preserves_commits(
    repo_path: Path, before_head: str, after_head: str,
    target_base_oid: str, git: Callable[..., object],
) -> bool:
    """Require the Manager's rebase to replay every original patch unchanged."""
    try:
        if git(repo_path, 'merge-base', '--is-ancestor', target_base_oid,
               after_head, check=False).returncode != 0:
            return False
        if before_head == after_head:
            return True
        old_base = git(repo_path, 'merge-base', before_head,
                       target_base_oid).stdout.strip()
        before_count = int(git(repo_path, 'rev-list', '--count',
                               f'{old_base}..{before_head}').stdout.strip())
        after_count = int(git(repo_path, 'rev-list', '--count',
                              f'{target_base_oid}..{after_head}').stdout.strip())
        if before_count == 0:
            # A branch at the old base can only rebase by fast-forwarding to
            # the target. There is no patch series for range-diff to compare.
            return old_base == before_head and after_head == target_base_oid
        if before_count != after_count:
            return False
        comparison = git(repo_path, 'range-diff', '--no-color', '--no-patch',
                         f'{old_base}..{before_head}',
                         f'{target_base_oid}..{after_head}', check=False)
    except (OSError, RuntimeError, ValueError):
        return False
    if comparison.returncode:
        return False
    lines = comparison.stdout.splitlines()
    return len(lines) == before_count and all(
        re.match(r'^\s*\d+:\s+[0-9a-f]+\s+=\s+\d+:\s+[0-9a-f]+\s+', line)
        for line in lines
    )
