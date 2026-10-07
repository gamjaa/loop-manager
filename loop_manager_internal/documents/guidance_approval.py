"""Deterministic format and patch helpers for controlled AGENTS.md changes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


PROPOSAL_HEADING = "### Agent Guidance Proposal:"
DECISION_HEADING = "### Agent Guidance User Decision:"
DECISION_LOG_HEADING = "## Decision Log"
GUIDANCE_TARGET = "AGENTS.md"
END_USER_DECISION = "<!-- END USER DECISION -->"


class GuidanceFormatError(RuntimeError):
    """A guidance proposal or user decision is incomplete or ambiguous."""


@dataclass(frozen=True)
class GuidanceDecision:
    proposal_id: str
    decision: str
    revision: str
    patch: str
    line: int
    baseline: str = ""

    @property
    def identity(self) -> str:
        if self.baseline:
            return _identity(
                "loop-manager-guidance-decision-v2", self.proposal_id,
                self.decision, self.revision, self.patch, self.baseline,
            )
        return _identity(
            "loop-manager-guidance-decision-v1",
            self.proposal_id,
            self.decision,
            self.revision,
            self.patch,
        )


@dataclass(frozen=True)
class GuidanceProposal:
    proposal_id: str
    target: str
    scope: str
    reason: str
    impact: str
    patch: str
    line: int
    decision: GuidanceDecision

    @property
    def identity(self) -> str:
        return _identity(
            "loop-manager-guidance-proposal-v1",
            self.proposal_id,
            self.target,
            self.scope,
            self.reason,
            self.impact,
            self.patch,
        )


@dataclass(frozen=True)
class GuidanceDocument:
    proposals: tuple[GuidanceProposal, ...]
    structure_hash: str
    legacy_structure_hashes: tuple[str, ...] = ()

    def matches_structure_hash(self, expected: str) -> bool:
        """Also recognize a saved legacy boundary without discarding its suffix."""
        return expected == self.structure_hash or expected in self.legacy_structure_hashes


_PROPOSAL_ID = r"[a-z0-9][a-z0-9-]{0,63}"
_PROPOSAL_HEADING_RE = re.compile(
    rf"(?m)^### Agent Guidance Proposal: (?P<id>{_PROPOSAL_ID})[ \t]*$"
)
_DECISION_HEADING_RE = re.compile(
    rf"(?m)^### Agent Guidance User Decision: (?P<id>{_PROPOSAL_ID})[ \t]*$"
)
_ANY_GUIDANCE_HEADING_RE = re.compile(
    r"(?m)^### Agent Guidance (?:Proposal|User Decision):"
)
_PROPOSAL_BODY_RE = re.compile(
    r"\A[ \t]*Target:[ \t]*`(?P<target>[^`\r\n]+)`[ \t]*\n"
    r"[ \t]*Scope:[ \t]*`(?P<scope>project|common)`[ \t]*\n"
    r"[ \t]*Reason:[ \t]*(?P<reason>[^\r\n]*\S)[ \t]*\n"
    r"[ \t]*Impact:[ \t]*(?P<impact>[^\r\n]*\S)[ \t]*\n"
    r"[ \t]*Patch:[ \t]*\n```diff[ \t]*\n(?P<patch>(?:(?!^```[ \t]*(?:\n|\Z)).)*?)^```(?:[ \t]*\n)*\Z",
    re.DOTALL | re.MULTILINE,
)


def _outside_fences(text: str) -> str:
    """Mask fenced code while preserving offsets and line numbers."""
    lines = []
    fence = ""
    closing_indent = 3
    for line in text.splitlines(keepends=True):
        if fence:
            if re.fullmatch(
                r"[ \t]{0," + str(closing_indent) + "}" + re.escape(fence[0])
                + "{" + str(len(fence)) + r",}[ \t]*\n?", line,
            ):
                fence = ""
            lines.append(re.sub(r"[^\n]", " ", line))
        else:
            opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
            if opening:
                fence = opening.group(1)
                # Unified diff context begins with a space. Only a column-zero
                # fence closes our diff format; indented backticks are data.
                closing_indent = 0 if line[opening.end():].strip() == "diff" else 3
                lines.append(re.sub(r"[^\n]", " ", line))
            else:
                lines.append(line)
    return "".join(lines)


def _decision_fields(body: str, proposal_id: str) -> dict[str, str]:
    # Strip explanatory comments only outside code; patch bytes remain literal.
    outside = _outside_fences(body)
    # Backtick field values are literal, including legacy Revision strings.
    outside = re.sub(r"`[^`\n]*`", lambda match: " " * len(match.group()), outside)
    comments = list(re.finditer(r"<!--.*?-->", outside, re.DOTALL))
    for comment in reversed(comments):
        body = body[:comment.start()] + body[comment.end():]
    fields: dict[str, str] = {}
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.fullmatch(r"[ \t]*Patch:[ \t]*\n", line):
            patch = re.fullmatch(
                r"```diff[ \t]*\n(?P<patch>(?:(?!^```[ \t]*(?:\n|\Z)).)*?)^```[ \t]*(?:\n[ \t]*)*\Z",
                "".join(lines[index + 1:]), re.DOTALL | re.MULTILINE,
            )
            if patch is None:
                raise GuidanceFormatError(f"User Decision diff 블록 형식 오류: {proposal_id}")
            fields["patch"] = patch.group("patch")
            break
        match = re.fullmatch(
            r"[ \t]*(Decision|Revision|Baseline|Comment):[ \t]*`([^`\r\n]*)`[ \t]*\n?", line,
        )
        if match is None:
            raise GuidanceFormatError(f"User Decision 필드 형식 오류: {proposal_id}: {line.strip()}")
        key = match.group(1).lower()
        if key in fields:
            raise GuidanceFormatError(f"User Decision {key} 필드 중복: {proposal_id}")
        fields[key] = match.group(2)
    if "decision" not in fields or "patch" not in fields:
        raise GuidanceFormatError(f"User Decision Decision·Patch 누락: {proposal_id}")
    if fields["decision"] not in ("pending", "approve", "reject", "replace"):
        raise GuidanceFormatError(f"User Decision Decision 값 오류: {proposal_id}")
    if "baseline" in fields and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields["baseline"]):
        raise GuidanceFormatError(f"User Decision Baseline 전체 SHA 형식 오류: {proposal_id}")
    return fields


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_patch(value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized.strip():
        return ""
    return normalized.rstrip("\n") + "\n"


def _identity(prefix: str, *values: str) -> str:
    digest = hashlib.sha256()
    digest.update(prefix.encode("ascii"))
    digest.update(b"\0")
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def guidance_patch_hash(target: str, patch: str) -> str:
    """Bind one normalized patch digest to its normalized target path."""

    return _identity(
        "loop-manager-guidance-patch-v1",
        normalize_target(target),
        _normalize_patch(patch),
    )


def content_hash(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def normalize_target(target: str) -> str:
    normalized = target.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or normalized.startswith("./")
        or str(candidate) != GUIDANCE_TARGET
    ):
        raise GuidanceFormatError(
            f"guidance 대상은 저장소 루트의 {GUIDANCE_TARGET}만 허용됩니다: {target}"
        )
    return str(candidate)


def contains_guidance_sections(text: str) -> bool:
    return _ANY_GUIDANCE_HEADING_RE.search(_outside_fences(_normalize_text(text))) is not None


def _decision_log(text: str) -> tuple[str, int, int]:
    outside = _outside_fences(text)
    headings = list(re.finditer(r"(?m)^## Decision Log[ \t]*$", outside))
    if len(headings) != 1:
        raise GuidanceFormatError("guidance 제안에는 정확히 하나의 ## Decision Log가 필요합니다.")
    start = headings[0].end()
    # An explicit terminator starts a memo area, including Markdown headings.
    # Legacy blocks still end at the next top-level section.
    memo = False
    for event in re.finditer(
        r"(?m)^[ \t]*<!-- END USER DECISION -->[ \t]*$|^### Agent Guidance (?:Proposal|User Decision):.*$|^## (?!#).*$",
        outside[start:],
    ):
        line = event.group().strip()
        if line == END_USER_DECISION:
            memo = True
        elif line.startswith("### Agent Guidance"):
            memo = False
        elif not memo:
            end = start + event.start()
            return text[start:end], start, end
    return text[start:], start, len(text)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def parse_guidance_document(text: str) -> GuidanceDocument | None:
    """Parse paired proposal/decision blocks from one ExecPlan Decision Log."""

    normalized = _normalize_text(text)
    outside = _outside_fences(normalized)
    markers = list(re.finditer(r"(?m)^[ \t]*<!-- END USER DECISION -->[ \t]*$", outside))
    if not contains_guidance_sections(normalized):
        if markers:
            raise GuidanceFormatError("User Decision 밖의 종료 marker입니다.")
        return None
    decision_log, log_start, _log_end = _decision_log(normalized)
    visible_log = _outside_fences(decision_log)
    headings = sorted(
        (
            *(('proposal', match) for match in _PROPOSAL_HEADING_RE.finditer(visible_log)),
            *(('decision', match) for match in _DECISION_HEADING_RE.finditer(visible_log)),
        ),
        key=lambda item: item[1].start(),
    )
    actual_headings = list(_ANY_GUIDANCE_HEADING_RE.finditer(outside))
    valid_offsets = {log_start + heading.start() for _, heading in headings}
    for heading in actual_headings:
        if heading.start() not in valid_offsets:
            line = _line_number(normalized, heading.start())
            title = normalized[heading.start():].splitlines()[0]
            raise GuidanceFormatError(f"guidance ID 형식 또는 Decision Log 위치 오류 (line {line}): {title}")
    if any(not log_start <= marker.start() < _log_end for marker in markers):
        raise GuidanceFormatError("User Decision 밖의 종료 marker입니다.")
    if not headings:
        raise GuidanceFormatError(
            "guidance heading은 ## Decision Log 안에 있어야 합니다."
        )
    if any(marker.start() < log_start + headings[0][1].start() for marker in markers):
        raise GuidanceFormatError("User Decision 밖의 종료 marker입니다.")
    if len(headings) % 2:
        raise GuidanceFormatError(
            "guidance proposal 바로 뒤에는 같은 ID의 User Decision이 필요합니다."
        )
    for index in range(0, len(headings), 2):
        proposal_kind, proposal_heading = headings[index]
        decision_kind, decision_heading = headings[index + 1]
        if (
            proposal_kind != "proposal"
            or decision_kind != "decision"
            or proposal_heading.group("id") != decision_heading.group("id")
        ):
            raise GuidanceFormatError(
                "guidance proposal과 같은 ID의 User Decision을 순서대로 작성해야 합니다."
            )
    outside_log = normalized[:log_start] + normalized[log_start + len(decision_log):]
    if contains_guidance_sections(outside_log):
        raise GuidanceFormatError(
            "guidance heading은 ## Decision Log 안에만 기록해야 합니다."
        )

    raw_proposals: dict[str, tuple[re.Match[str], re.Match[str]]] = {}
    raw_decisions: dict[str, tuple[re.Match[str], dict[str, str]]] = {}
    structure_parts: list[str] = []
    cursor = 0
    for index, (kind, heading) in enumerate(headings):
        body_start = heading.end()
        if decision_log[body_start:body_start + 1] == "\n":
            body_start += 1
        body_end = (
            headings[index + 1][1].start()
            if index + 1 < len(headings)
            else len(decision_log)
        )
        body = decision_log[body_start:body_end]
        proposal_id = heading.group("id")
        structure_parts.append(decision_log[cursor:heading.start()])
        body_markers = list(re.finditer(
            r"(?m)^[ \t]*<!-- END USER DECISION -->[ \t]*$", _outside_fences(body),
        ))
        if body_markers and kind != "decision":
            raise GuidanceFormatError(f"User Decision 밖의 종료 marker: {proposal_id}")
        if len(body_markers) > 1:
            raise GuidanceFormatError(f"User Decision 종료 marker 중복: {proposal_id}")
        if body_markers:
            body = body[:body_markers[0].start()]
        if kind == "proposal":
            if proposal_id in raw_proposals:
                raise GuidanceFormatError(
                    f"guidance proposal ID가 중복됩니다: {proposal_id}"
                )
            match = _PROPOSAL_BODY_RE.fullmatch(body)
            if match is None:
                raise GuidanceFormatError(
                    f"guidance proposal 형식이 완전하지 않습니다: {proposal_id}"
                )
            raw_proposals[proposal_id] = (heading, match)
            structure_parts.append(decision_log[heading.start():body_end])
        else:
            if proposal_id in raw_decisions:
                raise GuidanceFormatError(
                    f"guidance User Decision ID가 중복됩니다: {proposal_id}"
                )
            try:
                fields = _decision_fields(body, proposal_id)
            except GuidanceFormatError as error:
                line = _line_number(normalized, log_start + heading.start())
                raise GuidanceFormatError(f"{error} (line {line})") from error
            raw_decisions[proposal_id] = (heading, fields)
            structure_parts.append(
                f"### Agent Guidance User Decision: {proposal_id}\n"
                "<USER-DECISION>\n"
            )
        cursor = body_end
    structure_parts.append(decision_log[cursor:])

    if set(raw_proposals) != set(raw_decisions):
        missing_decisions = sorted(set(raw_proposals) - set(raw_decisions))
        missing_proposals = sorted(set(raw_decisions) - set(raw_proposals))
        detail = []
        if missing_decisions:
            detail.append("결정 누락: " + ", ".join(missing_decisions))
        if missing_proposals:
            detail.append("제안 누락: " + ", ".join(missing_proposals))
        raise GuidanceFormatError(
            "guidance 제안과 User Decision이 대응하지 않습니다 ("
            + "; ".join(detail) + ")."
        )

    proposals: list[GuidanceProposal] = []
    for proposal_id, (proposal_heading, proposal_match) in sorted(
        raw_proposals.items(), key=lambda item: item[1][0].start()
    ):
        decision_heading, decision_match = raw_decisions[proposal_id]
        proposal_patch = _normalize_patch(proposal_match.group("patch"))
        if not proposal_patch:
            raise GuidanceFormatError(
                f"guidance proposal patch가 비어 있습니다: {proposal_id}"
            )
        target = normalize_target(proposal_match.group("target"))
        validate_patch_target(proposal_patch, target)
        decision_kind = decision_match["decision"]
        revision = decision_match.get("revision", "").strip()
        decision_patch = _normalize_patch(decision_match["patch"])
        baseline = decision_match.get("baseline", "")
        if decision_patch:
            validate_patch_target(decision_patch, target)
        if decision_kind == "pending" and (revision or baseline):
            raise GuidanceFormatError(
                f"pending User Decision의 Revision·Baseline은 비어 있어야 합니다: {proposal_id}"
            )
        if decision_kind == "approve" and decision_patch not in ("", proposal_patch):
            raise GuidanceFormatError(
                f"approve Patch가 원안과 다릅니다. 수정본은 replace로 확정해야 합니다: {proposal_id}"
            )
        if decision_kind == "replace" and not decision_patch:
            raise GuidanceFormatError(f"replace User Decision에는 Patch가 필요합니다: {proposal_id}")
        # Preserve legacy identities: displayed originals and empty blocks have
        # the same meaning. Only a replacement or edited review patch is bound.
        if decision_kind != "replace" and decision_patch == proposal_patch:
            decision_patch = ""
        if decision_kind == "reject":
            decision_patch = ""
        decision = GuidanceDecision(
            proposal_id=proposal_id,
            decision=decision_kind,
            revision=revision,
            patch=decision_patch,
            line=_line_number(normalized, log_start + decision_heading.start()),
            baseline=baseline,
        )
        proposals.append(
            GuidanceProposal(
                proposal_id=proposal_id,
                target=target,
                scope=proposal_match.group("scope"),
                reason=proposal_match.group("reason").strip(),
                impact=proposal_match.group("impact").strip(),
                patch=proposal_patch,
                line=_line_number(normalized, log_start + proposal_heading.start()),
                decision=decision,
            )
        )

    structure = (
        normalized[:log_start]
        + "".join(structure_parts)
        + normalized[log_start + len(decision_log):]
    )
    structure_hash = _identity("loop-manager-guidance-plan-v1", structure)
    legacy_structure_hashes = []
    for section in re.finditer(r"(?m)^## (?!#)", visible_log):
        if any(heading.start() > section.start() for _, heading in headings):
            continue
        # A marker/memo may precede a formerly protected section. Compare its
        # exact suffix against the saved legacy hash; never rewrite that hash
        # or infer an existing section from its title. New memo headings before
        # the original suffix do not prevent this exact comparison.
        legacy_structure = (
            normalized[:log_start] + "".join(structure_parts)
            + normalized[log_start + section.start():]
        )
        legacy_structure_hashes.append(_identity("loop-manager-guidance-plan-v1", legacy_structure))
    return GuidanceDocument(tuple(proposals), structure_hash, tuple(legacy_structure_hashes))


def finalized_patch(proposal: GuidanceProposal) -> str | None:
    if proposal.decision.decision == "reject":
        return None
    if proposal.decision.decision == "approve":
        return proposal.patch
    if proposal.decision.decision == "replace":
        return proposal.decision.patch
    raise GuidanceFormatError(
        f"User Decision이 아직 확정되지 않았습니다: {proposal.proposal_id}"
    )


def validate_patch_target(patch: str, target: str) -> None:
    """Reject multi-file, mode-changing, binary, or path-escaping patches."""

    normalized_target = normalize_target(target)
    normalized = _normalize_patch(patch)
    if any(
        marker in normalized
        for marker in (
            "GIT binary patch",
            "Binary files ",
            "new file mode ",
            "deleted file mode ",
            "old mode ",
            "new mode ",
            "rename from ",
            "rename to ",
        )
    ):
        raise GuidanceFormatError("guidance patch는 기존 텍스트 파일 내용만 변경할 수 있습니다.")
    diffs = re.findall(r"(?m)^diff --git a/(\S+) b/(\S+)[ \t]*$", normalized)
    old_paths = re.findall(r"(?m)^--- a/(\S+)[ \t]*$", normalized)
    new_paths = re.findall(r"(?m)^\+\+\+ b/(\S+)[ \t]*$", normalized)
    if (
        diffs != [(normalized_target, normalized_target)]
        or old_paths != [normalized_target]
        or new_paths != [normalized_target]
        or re.search(r"(?m)^@@ -\d", normalized) is None
    ):
        raise GuidanceFormatError(
            f"guidance patch는 {normalized_target} 하나만 수정하는 unified diff여야 합니다."
        )


def apply_patch_to_content(original: bytes, target: str, patch: str) -> bytes:
    """Return Git's deterministic result without mutating the managed worktree."""

    normalized_target = normalize_target(target)
    normalized_patch = _normalize_patch(patch)
    validate_patch_target(normalized_patch, normalized_target)
    with tempfile.TemporaryDirectory(prefix="loop-manager-guidance-") as directory:
        root = Path(directory)
        destination = root / normalized_target
        destination.parent.mkdir(parents=True, exist_ok=True)
        crlf = b"\r\n" in original and b"\n" not in original.replace(b"\r\n", b"")
        destination.write_bytes(original.replace(b"\r\n", b"\n") if crlf else original)
        result = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
            input=normalized_patch.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=root,
            check=False,
        )
        if result.returncode:
            detail = result.stdout.decode("utf-8", errors="replace")[-3000:].strip()
            raise GuidanceFormatError(
                "guidance patch를 대상 기준 내용에 정확히 적용할 수 없습니다"
                + (f": {detail}" if detail else ".")
            )
        contents = destination.read_bytes()
        return contents.replace(b"\n", b"\r\n") if crlf else contents
