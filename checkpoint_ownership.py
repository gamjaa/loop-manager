"""Proof of a published plan checkpoint's last accepted remote head."""

from __future__ import annotations

import sqlite3
import re
from pathlib import Path
from typing import Callable
import blocked_checkpoint


def owned_remote_oid(
    connection: sqlite3.Connection, repo_path: Path, repo: str,
    plan_id: str, branch: str, worktree: Path, remote_oid: str | None,
    git: Callable[..., object], *, before_head: str | None = None,
    recorded_oid: str | None = None,
    require_live: bool = True,
) -> str | None:
    """Return an OID only when durable identity and live origin both prove it."""
    if not remote_oid:
        return None
    row = connection.execute(
        "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
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
        elif (
            row['local_oid'] != published
            or row['commit_identity'] != blocked_checkpoint.checkpoint_identity(
                plan_id=plan_id, reason_code=str(row['reason_code']),
                evidence=str(row['evidence']), parent_oid=published,
                tree_oid=tree, included_paths=(),
            )
        ):
            return None
        if git(repo_path, 'merge-base', '--is-ancestor', published,
               remote_oid, check=False).returncode != 0:
            return None
        if before_head and git(repo_path, 'merge-base', '--is-ancestor',
                               remote_oid, before_head, check=False).returncode != 0:
            return None
        if not require_live:
            return remote_oid
        observed = git(repo_path, 'ls-remote', '--heads', 'origin',
                       f'refs/heads/{branch}', check=False)
    except (OSError, RuntimeError):
        return None
    lines = [line.split() for line in observed.stdout.splitlines() if line.strip()]
    if observed.returncode or lines != [[remote_oid, f'refs/heads/{branch}']]:
        return None
    return remote_oid


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
        if before_count == 0 or before_count != after_count:
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
