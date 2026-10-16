"""documents pr description boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import WorkerPublication
from pathlib import Path
import hashlib
import json
import tempfile

class DocumentReportError(RuntimeError):
    """The final diff needs a corrected Worker document explanation."""


COMMON_REPORT_START = '<!-- loop-manager:common-guidance:start -->'
COMMON_REPORT_END = '<!-- loop-manager:common-guidance:end -->'


def common_guidance_report(git, worktree: Path, base_sha: str, head_sha: str,
                           origin_url: str, summary: str, *, selected_execplan_path: str) -> str:
    """Report final document diffs except the selected ExecPlan."""
    fields = git(worktree, "diff", "--no-ext-diff", "--name-status", "-z", "--find-renames",
                 base_sha, head_sha).stdout.split("\0")
    changes, index = [], 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        count = 2 if status.startswith(("R", "C")) else 1
        paths = fields[index + 1:index + 1 + count]
        if len(paths) != count or any(not path for path in paths):
            raise RuntimeError("문서 최종 diff 경로를 확인할 수 없습니다.")
        if paths[-1] != selected_execplan_path and any(
            path.startswith("docs/") or path.lower().endswith((".md", ".rst", ".txt"))
            for path in paths
        ):
            changes.append((status, paths))
        index += 1 + count
    if not changes:
        return ""
    explanations, errors = {}, []
    for line in summary.splitlines():
        if not line.startswith("DOCUMENT_CHANGE:"):
            continue
        try:
            item = json.loads(line.partition(":")[2])
            if isinstance(item, dict) and item.get("path") == selected_execplan_path:
                continue
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("path", "summary", "reason", "impact", "patch_sha256")
            ) or item["path"] in explanations:
                raise ValueError("missing/duplicate document fields")
            explanations[item["path"]] = item
        except (ValueError, TypeError):
            errors.append("PR_SUMMARY의 DOCUMENT_CHANGE에 문서 경로·내용·이유·영향·patch_sha256을 보완하라.")
    final_paths = {paths[-1] for _, paths in changes}
    stale = explanations.keys() - final_paths
    if stale:
        errors.append("최종 diff에 없는 DOCUMENT_CHANGE를 제거하라: " + ", ".join(sorted(stale)))
    report = [COMMON_REPORT_START, "## 최종 문서 변경", ""]
    required = []
    for _, paths in changes:
        patch = git(worktree, "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                    base_sha, head_sha, "--", *paths).stdout
        explanation = explanations.get(paths[-1])
        patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        required.append(f"{paths[-1]}; patch_sha256={patch_hash}")
        if explanation is None or explanation["patch_sha256"] != patch_hash:
            errors.append(f"최종 문서 설명 누락 또는 patch 변경: {paths[-1]}")
            continue
        report.extend([
            f"### {' → '.join(paths)}", "",
            f"- 변경 요약: {explanation['summary']}",
            f"- 이유: {explanation['reason']}",
            f"- 영향: {explanation['impact']}", "",
        ])
    if errors:
        raise DocumentReportError("\n".join([
            *sorted(set(errors)), f"base={base_sha}, head={head_sha}",
            "최종 응답의 PR_SUMMARY에 선택된 ExecPlan을 제외한 변경 문서 전체의 DOCUMENT_CHANGE를 함께 제출하라. "
            "유효한 기존 설명도 유지하고 현재 diff의 주요 내용·수정 이유·제품/설계 영향을 포함하라.",
            *required,
        ]))
    report.append(COMMON_REPORT_END)
    return "\n".join(report)


def refresh_common_guidance_pr(command, worktree: Path, state_dir: Path,
                               pull_request, report: str) -> None:
    """Refresh only Manager-owned reporting when reattaching an open PR."""
    if pull_request.get('state') != 'OPEN':
        return
    response = command(['gh', 'pr', 'view', str(pull_request['url']), '--json',
                        'body,headRefOid,state'], cwd=worktree)
    try:
        current = json.loads(response.stdout)
        if (response.returncode or current['state'] != 'OPEN'
                or current['headRefOid'] != pull_request['headRefOid']
                or not isinstance(current['body'], str)):
            raise ValueError('PR identity/body changed')
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError('문서 보고를 갱신할 PR의 현재 HEAD·본문을 확인할 수 없습니다.') from error
    body = current['body']
    if body.count(COMMON_REPORT_START) != body.count(COMMON_REPORT_END) or body.count(COMMON_REPORT_START) > 1:
        raise RuntimeError('PR의 문서 보고 경계가 손상됐습니다.')
    if COMMON_REPORT_START in body:
        first, end = body.index(COMMON_REPORT_START), body.index(COMMON_REPORT_END)
        if first > end:
            raise RuntimeError('PR의 문서 보고 경계 순서가 다릅니다.')
        last = end + len(COMMON_REPORT_END)
        updated = body[:first] + report + body[last:]
    elif report:
        updated = body.rstrip() + '\n\n' + report + '\n'
    else:
        return
    if updated == body:
        return
    directory = state_dir / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                     suffix='.md', delete=False) as stream:
        stream.write(updated)
        path = Path(stream.name)
    try:
        result = command(['gh', 'pr', 'edit', str(pull_request['url']), '--body-file', str(path)], cwd=worktree)
        if result.returncode:
            raise RuntimeError('PR의 최종 문서 보고 갱신이 실패했습니다.')
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class BuildPrBodyEffects:
    """Named effects for build_pr_body; no entry-point/module access."""
    execplan_purpose: Callable[..., object]
    immutable_github_blob_url: Callable[..., object]


def build_pr_body(
    effects: BuildPrBodyEffects, worker: WorkerPublication,
    plan: dict[str, object],
    plan_path: str,
    plan_text: str,
    origin_url: str,
    plan_sha: str,
    *, common_guidance_change: str = "",
) -> str:
    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    plan_link = effects.immutable_github_blob_url(origin_url, plan_sha, plan_path)
    sources = plan["sources"]
    assert isinstance(sources, list)
    source_lines = [
        f"- `{source}` (Manager verify 입력 목록 유지 규칙 v1)" if source == "manager:verify-inputs" else
        f"- [{source}]({effects.immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            "\n".join(line for line in worker.summary.splitlines() if not line.startswith("DOCUMENT_CHANGE:")),
            common_guidance_change,
            "",
            "## ExecPlan",
            f"- [{plan_id}: {plan_title}]({plan_link})",
            f"- Purpose: {effects.execplan_purpose(plan_text)}",
            "",
            "## Sources",
            *source_lines,
        ]
    )
