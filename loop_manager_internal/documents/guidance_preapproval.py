"""게시된 ExecPlan의 사용자 결정을 Worker 실행 전에 기존 승인 기록에 결합한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess

from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.documents.guidance_approval import (
    apply_patch_to_content, content_hash, finalized_patch, guidance_patch_hash,
    parse_guidance_document,
)
from loop_manager_internal.documents.guidance_service import (
    _guidance_batch_id, _guidance_document_matches_rows, _guidance_target_state,
)


@dataclass(frozen=True)
class PrepareGuidancePreapprovalsEffects:
    _guidance_document: Callable[..., object]
    _guidance_rows: Callable[..., object]
    _invalidate_guidance_approval: Callable[..., object]
    _validate_guidance_proposal_scope: Callable[..., object]
    git: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def prepare_guidance_preapprovals(
    effects: PrepareGuidancePreapprovalsEffects, connection: sqlite3.Connection,
    repo_path: Path, repo: str, worktree: Path, plan: Mapping[str, object],
) -> bool:
    """사전 결정을 한 번 등록한다. 기준 변경으로 차단했으면 True를 반환한다."""

    rows = effects._guidance_rows(connection, repo, str(plan["plan_id"]))
    if rows:
        # 등록 직후 중단돼 아직 Worker scope를 시작하지 않은 경우에도 기준을 재확인한다.
        pending = tuple(row for row in rows if not int(row["applied"]))
        if pending and not plan["agent_snapshot"] and all(
            str(row["batch_id"]).startswith("preapproval:") for row in pending
        ):
            if any(row["invalidated_decision_hash"] for row in pending):
                return True
            local = effects._guidance_document(worktree, str(plan["path"]))
            if local is None:
                raise AgentScopeError("등록된 사전 승인 제안·결정이 작업 파일에서 사라졌습니다.")
            proposals, new = _guidance_document_matches_rows(local, rows, require_decisions=True)
            if new:
                raise AgentScopeError("Worker 시작 전 새 guidance 결정을 사전 승인에 추가할 수 없습니다.")
            changed = {}
            eol_changes = {}
            for row in pending:
                target = str(row["target_path"])
                contents, mode = _guidance_target_state(worktree, target)
                baseline = bytes(row["baseline_content"])
                if contents.replace(b"\r\n", b"\n") != baseline.replace(b"\r\n", b"\n") or mode != int(row["baseline_mode"]):
                    changed[target] = (contents, mode)
                elif contents != baseline:
                    eol_changes[target] = contents
            if changed:
                with connection:
                    for target, (contents, mode) in changed.items():
                        connection.execute(
                            "UPDATE guidance_approvals SET baseline_content = ?, baseline_mode = ? "
                            "WHERE repo = ? AND plan_id = ? AND target_path = ? AND applied = 0",
                            (contents, mode, repo, plan["plan_id"], target),
                        )
                    effects._invalidate_guidance_approval(
                        connection, repo, worktree, plan, local,
                        tuple(proposals[str(row["proposal_id"])] for row in pending if row["target_path"] in changed),
                        "", "사전 승인 등록 뒤 Worker 시작 전에 기준 파일이 변경됐습니다. 새 User Decision이 필요합니다.",
                    )
                return True
            if eol_changes:
                with connection:
                    for target, baseline in eol_changes.items():
                        updated_contents = baseline
                        for row in pending:
                            if row["target_path"] == target:
                                patch = finalized_patch(proposals[str(row["proposal_id"])])
                                if patch is not None:
                                    updated_contents = apply_patch_to_content(updated_contents, target, patch)
                        connection.execute(
                            "UPDATE guidance_approvals SET baseline_content = ?, expected_content_hash = ? "
                            "WHERE repo = ? AND plan_id = ? AND target_path = ? AND applied = 0",
                            (baseline, content_hash(updated_contents), repo, plan["plan_id"], target),
                        )
        return False
    revision = str(
        (plan["current_revision_sha"] if "current_revision_sha" in plan.keys() else None)
        or plan["plan_sha"] or ""
    )
    if not revision:
        return False
    published = effects.git(repo_path, "show", f"{revision}:{plan['path']}")
    source = parse_guidance_document(published.stdout)
    if source is None or all(p.decision.decision == "pending" for p in source.proposals):
        return False
    if any(p.decision.decision == "pending" for p in source.proposals):
        raise AgentScopeError("게시된 사전 승인 배치에는 pending 결정을 섞을 수 없습니다.")
    baseline_revisions = {p.decision.baseline for p in source.proposals}
    if len(baseline_revisions) != 1 or not all(baseline_revisions):
        raise AgentScopeError("사전 승인 배치에는 같은 기준 commit의 Baseline이 필요합니다.")
    baseline_revision = next(iter(baseline_revisions))
    ancestor = effects.git(repo_path, "merge-base", "--is-ancestor", baseline_revision, revision, check=False)
    if ancestor.returncode:
        raise AgentScopeError("사전 승인 Baseline은 채택한 원격 기준 revision의 선행 commit이어야 합니다.")
    effects._validate_guidance_proposal_scope(
        source.proposals, common_allowed=effects.is_loop_manager_repository(repo_path),
    )
    local = effects._guidance_document(worktree, str(plan["path"]))
    if local is None or tuple(
        (p.identity, p.decision.identity) for p in local.proposals
    ) != tuple((p.identity, p.decision.identity) for p in source.proposals):
        raise AgentScopeError("작업 파일의 guidance 제안·결정이 게시된 사전 승인과 다릅니다.")

    # git show의 원본 bytes를 사용해 checkout 줄바꿈 외의 차이를 승인하지 않는다.
    baselines: dict[str, tuple[bytes, int]] = {}
    expected: dict[str, bytes] = {}
    stale_targets: set[str] = set()
    for proposal in source.proposals:
        target = proposal.target
        if target not in baselines:
            tree = effects.git(repo_path, "ls-tree", baseline_revision, "--", target).stdout
            if not tree.startswith(("100644 blob ", "100755 blob ")):
                raise AgentScopeError(f"사전 승인 기준은 기존 일반 파일이어야 합니다: {target}")
            shown = subprocess.run(
                ["git", "show", f"{baseline_revision}:{target}"], cwd=repo_path,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            contents, mode = _guidance_target_state(worktree, target)
            baselines[target] = (contents, mode)
            source_mode = 0o755 if tree.startswith("100755 ") else 0o644
            if contents.replace(b"\r\n", b"\n") != shown.stdout.replace(b"\r\n", b"\n") or mode != source_mode:
                stale_targets.add(target)
            # 불일치 시에도 원래 승인 patch가 원래 기준에 적용되는지 확인한다.
            expected[target] = shown.stdout if target in stale_targets else contents
        patch = finalized_patch(proposal)
        if patch is not None:
            expected[target] = apply_patch_to_content(expected[target], target, patch)

    batch = "preapproval:" + _guidance_batch_id(repo, str(plan["plan_id"]), source, source.proposals)
    with connection:
        for sequence, proposal in enumerate(source.proposals):
            baseline, mode = baselines[proposal.target]
            patch = finalized_patch(proposal)
            connection.execute(
                """
                INSERT INTO guidance_approvals (
                    repo, plan_id, proposal_id, proposal_hash, target_path,
                    scope, proposal_patch, proposal_patch_hash,
                    proposal_line, decision_line, batch_id, sequence,
                    baseline_content, baseline_mode, decision, decision_revision,
                    decision_hash, approved_patch, approved_patch_hash, expected_content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo, plan["plan_id"], proposal.proposal_id, proposal.identity,
                 proposal.target, proposal.scope, proposal.patch,
                 guidance_patch_hash(proposal.target, proposal.patch),
                 proposal.line, proposal.decision.line, batch, sequence,
                 baseline, mode, proposal.decision.decision, proposal.decision.revision,
                 proposal.decision.identity, patch,
                 guidance_patch_hash(proposal.target, patch or ""),
                 content_hash(expected[proposal.target])),
            )
        if stale_targets:
            effects._invalidate_guidance_approval(
                connection, repo, worktree, plan, local,
                tuple(p for p in local.proposals if p.target in stale_targets), "",
                "AGENTS.md가 사전 승인 당시 기준과 다릅니다. 최신 기준에 맞는 "
                "User Decision Revision 또는 정확한 대체 patch가 필요합니다.",
            )
    return bool(stale_targets)
