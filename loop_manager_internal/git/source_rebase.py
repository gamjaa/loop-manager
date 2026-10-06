"""Conservative replay of a user's clean base, independent of remote work.

Use the Manager journal's immutable intent/result protocol, but never offer
source changes as Agent mutation evidence. A failed invocation is sealed before
an owned abort; an interruption without that receipt preserves the operation.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets

from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.recovery.checkpoint_ownership import rebase_preserves_commits
from loop_manager_internal.runtime.contracts import BaseWorktreeSyncResult
from loop_manager_internal.runtime.runtime_identity import _canonical_path


def _observe(effects, repo: Path, base: str) -> dict:
    state = effects._source_transition_state(repo)
    paths = set(effects._nul_git_paths(repo, "ls-files", "-z"))
    paths.update(effects._nul_git_paths(repo, "ls-files", "--others", "-z"))
    # Include files even while the normal source snapshot short-circuits on an
    # operation or unmerged index. An abort must never discard a later edit.
    from .source_sync import _base_local_path_signature
    return {
        **state,
        "files": {path: _base_local_path_signature(repo, path) for path in sorted(paths)},
        "ref": effects.git(repo, "rev-parse", f"refs/heads/{base}").stdout.strip(),
        "remote": effects.git(repo, "rev-parse", f"refs/remotes/origin/{base}").stdout.strip(),
        "origin": effects.git(repo, "remote", "get-url", "origin").stdout.strip(),
        "head_reflog": effects.git(repo, "reflog", "show", "-1", "--format=%H%x00%gs", "HEAD").stdout,
        "branch_reflog": effects.git(repo, "reflog", "show", "-1", "--format=%H%x00%gs", f"refs/heads/{base}").stdout,
    }


def _completed(effects, repo: Path, intent: dict, current: dict) -> bool:
    before, expected = intent["before"], intent["expected"]
    return (
        current["branch"] == expected["base"] and not current["operations"]
        and current["head"] == current["ref"]
        and current["remote"] == expected["remote"]
        and current["origin"] == before["origin"]
        and current["changes"] == before["changes"]
        and not effects._base_worktree_index_error(effects._base_worktree_snapshot(repo))
        and journal.rebase_completion_matches(
            effects.git, repo, branch=expected["base"], head=current["head"],
            identity=intent["id"], target_base_oid=expected["remote"],
        )
        and current["head_reflog"].startswith(
            f"{current['head']}\0loop-manager-rebase:{intent['id']} (finish): "
        )
        and rebase_preserves_commits(
            repo, before["head"], current["head"], expected["remote"], effects.git,
        )
    )


def _owned_conflict(effects, repo: Path, intent: dict, current: dict) -> bool:
    before, expected = intent["before"], intent["expected"]
    names = {entry[0] for entry in current["operations"]}
    if (
        "rebase-merge" not in names or names - {"rebase-merge", "REBASE_HEAD", "AUTO_MERGE", "MERGE_MSG"}
        or current["branch"] is not None or current["ref"] != before["ref"]
        or current["remote"] != expected["remote"] or current["origin"] != before["origin"]
        or current["branch_reflog"] != before["branch_reflog"]
        or not journal.rebase_conflict_matches(effects.git, repo, head=current["head"], identity=intent["id"])
    ):
        return False
    directory = Path(effects.git(repo, "rev-parse", "--git-path", "rebase-merge").stdout.strip())
    if not directory.is_absolute():
        directory = repo / directory
    for name, value in (("head-name", f"refs/heads/{expected['base']}"),
                        ("orig-head", before["head"]), ("onto", expected["remote"])):
        path = directory / name
        if not path.is_file() or path.read_text().strip() != value:
            return False
    if (directory / "autostash").exists():
        return False
    affected = set(expected["affected"])
    return all(
        old.get(path) == new.get(path) or path in affected
        for old, new in ((before["files"], current["files"]), (before["index"], current["index"]))
        for path in old.keys() | new.keys()
    )


def _restore_failure(effects, repo: Path, slot: Path, intent: dict, current: dict) -> dict:
    receipt = slot / f"{intent['id']}.invocation.json"
    if not receipt.is_file():
        raise journal.MutationEvidenceError("source rebase 중단: invocation 증거가 없어 현재 작업을 보존합니다.")
    recorded = journal._read(receipt)
    if recorded.get("id") != intent["id"] or recorded.get("observed") != current or not recorded.get("returncode"):
        raise journal.MutationEvidenceError("source rebase 실패 뒤 상태가 변경되어 현재 작업을 보존합니다.")
    if current == intent["before"]:
        return current
    if not _owned_conflict(effects, repo, intent, current) or _observe(effects, repo, intent["expected"]["base"]) != current:
        raise journal.MutationEvidenceError("source rebase abort 소유권을 증명할 수 없어 현재 작업을 보존합니다.")
    environment = {**os.environ, "GIT_REFLOG_ACTION": f"loop-manager-rebase:{intent['id']}"}
    effects.git(repo, "rebase", "--abort", env=environment)
    restored = _observe(effects, repo, intent["expected"]["base"])
    # Abort changes HEAD's reflog, so compare the actual ref, index and every
    # file against the frozen start; retain the post-abort reflog in the result.
    if any(restored[key] != intent["before"][key] for key in restored if key != "head_reflog"):
        raise journal.MutationEvidenceError("source rebase abort 결과 불일치; 복구 근거를 보존합니다.")
    return restored


def recover(effects, repo: Path, state_dir: Path, base: str) -> BaseWorktreeSyncResult | None:
    canonical = str(_canonical_path(repo))
    slot = journal._slot(state_dir, canonical, "source_rebase", canonical)
    active = slot / "active.json"
    if not active.exists():
        return None
    intent = journal._read(active)
    if (intent.get("repo") != canonical or intent.get("target") != canonical
            or intent.get("kind") != "source_rebase" or intent["expected"].get("base") != base
            or journal._read(slot / f"{intent['id']}.intent.json") != intent):
        raise journal.MutationEvidenceError("source rebase intent identity 불일치")
    current = _observe(effects, repo, base)
    remote = intent["expected"]["remote"]
    result_path = slot / f"{intent['id']}.result.json"
    if result_path.exists():
        result = journal._read(result_path)
        if (result.get("id") != intent["id"] or result.get("status") not in {"complete", "aborted"}
                or result.get("candidates") != {} or result["observed"] != current):
            raise journal.MutationEvidenceError("source rebase 확정 뒤 상태 변경; 기존 근거를 보존합니다.")
        journal._finish(slot, active, intent, current, {}, result["status"])
        return BaseWorktreeSyncResult("updated" if result["status"] == "complete" else "failed", "source rebase 확정 복구", remote)
    if _completed(effects, repo, intent, current):
        journal._finish(slot, active, intent, current, {}, "complete")
        return BaseWorktreeSyncResult("updated", "로컬 commit 보존 rebase 확정 복구", remote)
    before = intent["before"]
    if all(current[key] == before[key] for key in current if key not in {"head_reflog", "remote"}):
        if current["head_reflog"] == before["head_reflog"] or current["head_reflog"].startswith(
            f"{before['head']}\0loop-manager-rebase:{intent['id']} (abort): "
        ):
            journal._finish(slot, active, intent, current, {}, "aborted")
            return BaseWorktreeSyncResult("failed", "source rebase 미적용 또는 소유 abort 확정; 시작 전 작업 보존", remote)
    restored = _restore_failure(effects, repo, slot, intent, current)
    journal._finish(slot, active, intent, restored, {}, "aborted")
    return BaseWorktreeSyncResult("failed", "source rebase 실패; 시작 전 작업 보존 복구", remote)


def synchronize(effects, repo: Path, state_dir: Path | None, base: str,
                remote: str, before, owner: str) -> BaseWorktreeSyncResult:
    if state_dir is None:
        return BaseWorktreeSyncResult("skipped", "diverged: durable 소유 intent 경로 없음", remote)
    if any(kind != "ignored" for kind, _path, _signature in before.changes):
        return BaseWorktreeSyncResult("skipped", "diverged: 미커밋 작업 보존", remote)
    fork = effects.git(repo, "merge-base", before.head, remote).stdout.strip()
    if effects.git(repo, "rev-list", "--merges", f"{fork}..{before.head}").stdout.strip():
        return BaseWorktreeSyncResult("skipped", "diverged: merge commit 보존을 증명할 수 없음", remote)
    affected = journal.rebase_affected_paths(effects.git, repo, before.head, remote)
    if any(effects._base_paths_overlap(path, changed) for _kind, path, _signature in before.changes for changed in affected):
        return BaseWorktreeSyncResult("skipped", "diverged: rebase 경로와 겹치는 ignored 파일 보존", remote)
    canonical = str(_canonical_path(repo))
    slot = journal._slot(state_dir, canonical, "source_rebase", canonical)
    if slot.exists():
        # A failed attempt is never repeated at the same actual local/remote
        # inputs. New commits, file/index changes or origin changes allow a check.
        current = _observe(effects, repo, base)
        for path in slot.glob("*.result.json"):
            result = journal._read(path)
            journal._read(slot / f"{result['id']}.intent.json")
            if result["status"] == "aborted" and result["observed"] == current:
                return BaseWorktreeSyncResult("skipped", "diverged: 동일 입력의 실패 rebase 반복 보류", remote)
    if effects._base_worktree_snapshot(repo) != before:
        return BaseWorktreeSyncResult("skipped", "diverged: eligibility 뒤 상태가 변경됨", remote)
    start = _observe(effects, repo, base)
    source_state = {"branch": before.branch, "head": before.head,
                    "index": {path: list(entries) for path, entries in before.index},
                    "changes": [list(entry) for entry in before.changes],
                    "operations": [list(entry) for entry in before.operations]}
    if (any(start[key] != value for key, value in source_state.items())
            or start["ref"] != before.head or start["remote"] != remote):
        return BaseWorktreeSyncResult("skipped", "diverged: rebase 직전 상태 변경", remote)
    root = state_dir / "manager-git-journal"
    root.mkdir(parents=True, exist_ok=True)
    journal._sync_directory(state_dir)
    slot.mkdir(parents=True, exist_ok=True)
    journal._sync_directory(root)
    identity = secrets.token_hex(16)
    intent = {"version": 1, "id": identity, "owner": owner, "kind": "source_rebase",
              "repo": canonical, "target": canonical, "before": start,
              "expected": {"base": base, "remote": remote, "affected": affected}}
    journal._write_new(slot / f"{identity}.intent.json", intent)
    journal._write_new(slot / "active.json", intent)
    if _observe(effects, repo, base) != start:
        raise journal.MutationEvidenceError("source rebase intent 뒤 동시 변경; 현재 상태 보존")
    environment = {**os.environ, "GIT_REFLOG_ACTION": f"loop-manager-rebase:{identity}",
                   "GIT_SEQUENCE_EDITOR": ":", "GIT_EDITOR": ":"}
    result = effects.git(
        repo, "-c", "rebase.updateRefs=false", "-c", "rebase.autoSquash=false",
        "rebase", "--merge", "--no-autostash", "--no-fork-point", "--reapply-cherry-picks",
        "--keep-empty", "--empty=keep", "--onto", remote, fork, base,
        env=environment, check=False,
    )
    observed = _observe(effects, repo, base)
    journal._write_new(slot / f"{identity}.invocation.json", {
        "id": identity, "returncode": result.returncode, "observed": observed,
        "diagnostic": result.stdout[-2000:].strip(),
    })
    recovered = recover(effects, repo, state_dir, base)
    if recovered is None:
        raise journal.MutationEvidenceError("source rebase 활성 intent가 없어 확정을 보류합니다.")
    if result.returncode:
        return BaseWorktreeSyncResult("failed", recovered.reason + ": " + result.stdout[-2000:].strip(), remote)
    return BaseWorktreeSyncResult("updated", "diverged: 원격 base 위로 로컬 commit 보존 rebase", remote)
