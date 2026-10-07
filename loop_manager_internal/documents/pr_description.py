"""documents pr description boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import WorkerPublication
from loop_manager_internal.agents.common_guidance import GUIDANCE_PATHS
from pathlib import Path
import hashlib
import json
import re
import tempfile
from loop_manager_internal.git.git_adapter import github_repository

COMMON_REPORT_START = '<!-- loop-manager:common-guidance:start -->'
COMMON_REPORT_END = '<!-- loop-manager:common-guidance:end -->'


def common_guidance_report(git, worktree: Path, base_sha: str, head_sha: str,
                           origin_url: str, summary: str) -> str:
    """Report the final candidate, including renamed/deleted instruction files."""
    fields = git(worktree, "diff", "--no-ext-diff", "--name-status", "-z", "--find-renames",
                 base_sha, head_sha).stdout.split("\0")
    changes, index = [], 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        count = 2 if status.startswith(("R", "C")) else 1
        paths = fields[index + 1:index + 1 + count]
        if len(paths) != count:
            raise RuntimeError("공통 지침 최종 diff 경로를 확인할 수 없습니다.")
        if any(path in GUIDANCE_PATHS for path in paths):
            changes.append((status, paths))
        index += 1 + count
    if not changes:
        return ""
    affected = list(dict.fromkeys(p for _, paths in changes for p in paths))
    path = next(path for path in affected if path in GUIDANCE_PATHS)
    patch = git(worktree, "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                base_sha, head_sha, "--", *affected).stdout
    url = "https://github.com/" + github_repository(origin_url)
    link = f"{url}/compare/{base_sha}...{head_sha}#diff-{hashlib.sha256(path.encode()).hexdigest()}"
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", patch)), default=0))
    return "\n".join([
        COMMON_REPORT_START,
        "## 공통·역할별 실행 지침 변경", "",
        f"- 경로·변경 사실: {', '.join(status + ' ' + ' → '.join(paths) for status, paths in changes)}",
        f"- 이유: {summary}",
        "- 영향: Merge 뒤 새 runtime의 새 작업에 적용한다. 현재 작업의 Worker·Reviewer·재개 지침 snapshot과 프로젝트 AGENTS 승인은 보존한다.",
        f"- [최종 내용 diff]({link})", "",
        "<details><summary>최종 후보의 실행 지침 변경 내용</summary>", "",
        fence + "diff", patch.rstrip(), fence, "", "</details>", COMMON_REPORT_END,
    ])


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
        raise RuntimeError('공통 지침 보고를 갱신할 PR의 현재 HEAD·본문을 확인할 수 없습니다.') from error
    body = current['body']
    if body.count(COMMON_REPORT_START) != body.count(COMMON_REPORT_END) or body.count(COMMON_REPORT_START) > 1:
        raise RuntimeError('PR의 공통 지침 보고 경계가 손상됐습니다.')
    if COMMON_REPORT_START in body:
        first, end = body.index(COMMON_REPORT_START), body.index(COMMON_REPORT_END)
        if first > end:
            raise RuntimeError('PR의 공통 지침 보고 경계 순서가 다릅니다.')
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
            raise RuntimeError('PR의 최종 공통 지침 보고 갱신이 실패했습니다.')
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
        f"- [{source}]({effects.immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            worker.summary,
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
