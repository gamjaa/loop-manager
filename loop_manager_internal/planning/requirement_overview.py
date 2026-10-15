"""Build a discovery overview from active Requirements at one pinned Git revision."""
from __future__ import annotations

import json
import re
from pathlib import Path

from loop_manager_internal.catalog.frontmatter import RequirementIdError, requirement_identity


def requirement_summary(text: str) -> str | None:
    """Read the first level-two Summary outside fenced code blocks."""
    fence = None
    fence_length = 0
    collecting = False
    content: list[str] = []
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if collecting:
                content.append(line)
            if (marker and marker[1][0] == fence
                    and len(marker[1]) >= fence_length and not marker[2].strip()):
                fence = None
            continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence, fence_length = marker[1][0], len(marker[1])
        else:
            heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", line)
            if heading:
                level = len(heading[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
                if collecting and level <= 2:
                    break
                if level == 2 and title == "Summary":
                    collecting = True
                    continue
        if collecting:
            content.append(line)
    return "\n".join(content).strip() or None


def instruction(repo: Path, revision: str, *, git_bytes) -> str:
    if not revision:
        raise RuntimeError("Requirement 목록의 원격 revision이 없습니다.")
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision, "--", "docs/requirements")
    requirements = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator or len(metadata.split()) != 3:
            raise RuntimeError("Requirement 목록의 원격 tree 형식이 유효하지 않습니다.")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeError as error:
            raise RuntimeError("Requirement 경로가 UTF-8이 아닙니다.") from error
        if not (path.startswith("docs/requirements/") and path.count("/") == 2 and path.endswith(".md")):
            continue
        mode, kind, oid = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise RuntimeError(f"Requirement가 일반 원격 파일이 아닙니다: {path}")
        try:
            text = git_bytes(repo, "cat-file", "blob", oid.decode("ascii")).decode("utf-8-sig")
            _, title = requirement_identity(text)
        except (UnicodeError, RequirementIdError) as error:
            raise RuntimeError(f"Requirement를 읽을 수 없습니다: {path} ({error})") from error
        item = {"title": title, "path": path}
        summary = requirement_summary(text)
        if summary is not None:
            item["summary"] = summary
        requirements.append(item)
    if not requirements:
        return ""
    return ("\n\n검토 revision의 전체 활성 Requirement 탐색 목록 (archive 제외):\n"
            + json.dumps({"revision": revision, "requirements": requirements}, ensure_ascii=False)
            + "\n목록과 Summary는 관련 문서를 찾는 안내다. 변경되지 않은 Requirement도 유효하다. "
              "관련되거나 관련성이 불확실한 문서, 제목만으로 판단하기 어려운 문서의 본문을 위 revision에서 확인하라. "
              "요약 부재만으로 무관하게 취급하거나 전문 전수 검토를 강제하지 마라. "
              "목표·조건·예외·Acceptance Criteria는 본문으로 판단하며 요약을 만들기 위해 Requirement를 수정하지 마라.")
