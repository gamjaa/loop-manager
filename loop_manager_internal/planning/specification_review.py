"""Pinned change context and full specification review result validation."""
from __future__ import annotations

from loop_manager_internal.application.result_normalization import bind_identity, tagged_json

import json
import re
import subprocess

from loop_manager_internal.catalog.execplan_catalog import is_active_execplan_path
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid


def receipt_matches(receipt, *, input_fingerprint: str,
                    specification_fingerprint: str, work_cycle: str) -> bool:
    return bool(valid_final_receipt(receipt)
        and receipt["outcome"] == "fulfilled" and receipt["confirmed_revision"]
        and receipt["work_cycle"] == work_cycle
        and receipt["input_fingerprint"] == input_fingerprint
        and receipt["specification_fingerprint"] == specification_fingerprint)


def current_receipt(receipt, *, repo, revision, input_fingerprint,
                    specification_fingerprint, work_cycle, archive_only) -> bool:
    if receipt_matches(receipt, input_fingerprint=input_fingerprint,
                       specification_fingerprint=specification_fingerprint,
                       work_cycle=work_cycle):
        return True
    # Callers first match the durable verified plan declarations. Only exact,
    # content-preserving Requirement/ExecPlan archive moves may bridge inputs.
    return bool(valid_final_receipt(receipt)
        and receipt["outcome"] == "fulfilled" and receipt["confirmed_revision"]
        and receipt["work_cycle"] == work_cycle
        and archive_only(repo, receipt["revision"], revision,
                         verified_plan_declarations=True, allow_execplan_only=True))


def valid_final_receipt(receipt) -> bool:
    if receipt is None or receipt["purpose"] != "final" or not receipt["result"]:
        return False
    try:
        value = tagged_json(receipt["result"], "FINAL_SPECIFICATION_REVIEW")
        if isinstance(value, dict):
            value = bind_identity(value, {key: receipt[key] for key in (
                "revision", "input_fingerprint", "specification_fingerprint", "work_cycle")})
        return bool(isinstance(value, dict) and value.get("outcome") == receipt["outcome"] == "fulfilled"
            and isinstance(value.get("reason"), str) and value["reason"].strip()
            and isinstance(value.get("evidence"), list) and value["evidence"]
            and all(value.get(key) == receipt[key] for key in (
                "revision", "input_fingerprint", "specification_fingerprint", "work_cycle")))
    except (TypeError, ValueError):
        return False


def comparison_revision(receipt, git, repo, revision) -> str | None:
    if receipt is None or not receipt["confirmed_revision"] or receipt["result"] is None or not receipt["outcome"]:
        return None
    previous = str(receipt["revision"])
    try:
        if git.run(repo, "merge-base", "--is-ancestor", previous, revision,
                   check=False).returncode != 0:
            return None
        if (git.input_fingerprint(repo, previous) != receipt["input_fingerprint"]
                or git.specification_fingerprint(repo, previous) != receipt["specification_fingerprint"]):
            return None
    except (RuntimeError, subprocess.SubprocessError):
        return None
    return previous


def review_instruction(attempt, git, repo) -> str:
    identity = {key: attempt[key] for key in (
        "purpose", "revision", "input_fingerprint", "specification_fingerprint", "work_cycle")}
    text = "\n\n일반 planning 검토 입력: " + json.dumps(identity, ensure_ascii=False)
    if attempt["purpose"] == "delta":
        previous, current = attempt["comparison_revision"], attempt["revision"]
        changes = git.run(repo, "diff", "--name-status", "--find-renames", "-z", previous, current).stdout
        text += ("\n변경 비교 revision: " + str(previous)
            + "\n변경·삭제·rename 목록 (NUL 구분): " + json.dumps(changes, ensure_ascii=False)
            + "\n이 목록과 compact catalog부터 확인하고 git diff로 관련 변경을 읽어라. "
              "삭제·rename의 이전 경로·source·dependency·계약과 코드·문서 동시 변경 영향을 포함하라. "
              "영향받은 문서 절·코드·테스트·계획과 필요한 문맥을 읽고 불확실하면 범위를 넓혀라. "
              "무관한 변경 때문에 PRODUCT·ARCHITECTURE 전문과 저장소 전체를 관례적으로 다시 읽지 마라. "
              "새 계획 없음은 최종 제품 충족 확인이 아니다.")
    else:
        text += ("\n현재 revision의 PRODUCT(존재하면)·ARCHITECTURE와 docs/requirements 바로 아래 "
                 "활성 Requirement 전문을 읽고 코드·테스트·최신 검증 근거로 미충족 작업을 확인하라.")
    if attempt["purpose"] == "final":
        text += ("\n프로젝트 완료 후보의 최종 전문 확인이다. 최근 diff 밖의 제품 목표·장기 규칙·Non-Goal·설계 "
                 "제약과 누락을 확인하라. 미충족 작업은 source에 연결된 새 계획으로, 필수 사용자 결정은 "
                 "질문·선택 영향을 가진 draft로 남겨라. Requirement별 판단과 유지보수 종료는 별도 계약이다. "
                 "파일을 읽었거나 catalog가 비었다는 이유만으로 충족을 선언하지 마라.\n"
                 "결과에 FINAL_SPECIFICATION_REVIEW: 한 줄 JSON을 정확히 하나 포함하라. "
                 "위 revision, input_fingerprint, specification_fingerprint, work_cycle 값을 그대로 포함하고 "
                 "outcome은 fulfilled|followup|needs_decision, evidence는 확인한 활성 명세·구현·테스트·검증 "
                 "파일 경로의 비어 있지 않은 배열, reason은 경로·규칙·확인 사실의 요약이며 비밀값이나 원문을 "
                 "포함하지 마라. fulfilled에는 미충족 계획·draft를 "
                 "남길 수 없다. followup에는 source에 연결된 새 계획, needs_decision에는 새 draft가 필요하다.")
    return text


def validate_final(attempt, result: str, batch, git, repo, *, manager_source_validator=None) -> str:
    try:
        value = tagged_json(result, "FINAL_SPECIFICATION_REVIEW")
        if not isinstance(value, dict):
            raise ValueError("최종 전문 확인은 JSON object여야 합니다.")
        value = bind_identity(value, {key: attempt[key] for key in (
            "revision", "input_fingerprint", "specification_fingerprint", "work_cycle")})
        outcome = value.get("outcome")
        if outcome not in {"fulfilled", "followup", "needs_decision"}:
            raise ValueError("최종 전문 확인 outcome이 유효하지 않습니다.")
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("최종 전문 확인 근거가 없습니다.")
        paths = value.get("evidence")
        if not isinstance(paths, list) or not paths:
            raise ValueError("최종 전문 확인 파일 근거가 없습니다.")
        for path in paths:
            if (not isinstance(path, str) or not path or path.startswith(("/", "docs/requirements/archive/", "docs/plans/archive/"))
                    or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                    or path == "docs/FUTURE_IDEAS.md" or is_active_execplan_path(path.encode())):
                raise ValueError("최종 전문 확인 파일 경로가 유효하지 않습니다.")
        tree = git.run(repo, "ls-tree", "-r", "-z", attempt["revision"], "--", *paths).stdout
        files = {}
        for entry in tree.split("\0"):
            if entry:
                metadata, path = entry.split("\t", 1)
                mode, kind, _oid = metadata.split(" ", 2)
                files[path] = (mode, kind)
        if any(files.get(path) not in {("100644", "blob"), ("100755", "blob")} for path in paths):
            raise ValueError("최종 전문 확인에 일반 원격 파일이 아닌 근거가 있습니다.")
        declarations = git.run(repo, "ls-tree", "-r", "--name-only", "-z", attempt["revision"],
                               "--", "docs/PRODUCT.md", "docs/ARCHITECTURE.md", "docs/requirements").stdout.split("\0")
        specifications = {path for path in declarations if path in {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
                          or re.fullmatch(r"docs/requirements/[^/]+\.md", path)}
        if not specifications or not specifications.issubset(paths):
            raise ValueError("최종 전문 확인에 활성 명세 전문의 근거가 누락됐습니다.")
        if not set(paths) - specifications:
            raise ValueError("최종 전문 확인에 구현·검증 근거가 없습니다.")
        plans = batch.plans if batch is not None else ()
        if outcome == "fulfilled" and (plans or batch is not None and (batch.revisions or batch.deletions or batch.restoration_moves)):
            raise ValueError("충족 결과에 미완료 후속 작업이 있습니다.")
        if outcome in {"followup", "needs_decision"} and not plans:
            raise ValueError("최종 확인의 미충족 작업에 후속 계획이 없습니다.")
        if outcome == "needs_decision" and not any(plan.get("draft") for _path, plan in plans):
            raise ValueError("사용자 판단 결과에 draft가 없습니다.")
        from loop_manager_internal.catalog.manager_sources import SOURCE
        for path, plan in plans:
            sources = plan.get("sources")
            if sources == [SOURCE]:
                if manager_source_validator is None:
                    raise ValueError("최종 확인의 Manager source에 등록 요청 검증이 없습니다.")
                manager_source_validator(path, plan)
            elif not sources or not set(sources).issubset(specifications):
                raise ValueError("최종 확인의 후속 계획에 활성 명세 source 연결이 없습니다.")
        return outcome
    except (ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        raise PlanningResultInvalid(str(error)) from error


def prepare_review(state, git, context, revision: str, *, final_required: bool):
    """Preserve the selected purpose across retry and owned-workspace rebase."""
    comparison = comparison_revision(
        state.planning_review(context.repo, "general"),
        git, context.repo_path, revision,
    )
    attempt = state.planning_review(context.repo, "attempt")
    work_cycle = state.review_work_cycle(context.repo)
    if (attempt is None or attempt["revision"] != revision
            or attempt["work_cycle"] != work_cycle):
        resume_final = (attempt is not None and attempt["purpose"] == "final"
            and state.final_review_candidate(context.repo,
                context.refactoring_review_interval, self_repository=context.self_repository))
        purpose = ("final" if final_required or resume_final
                   else "delta" if comparison else "full")
        state.begin_planning_review(
            context.repo, purpose=purpose, revision=revision,
            input_fingerprint=git.input_fingerprint(context.repo_path, revision),
            specification_fingerprint=git.specification_fingerprint(context.repo_path, revision),
            work_cycle=work_cycle, comparison_revision=comparison,
        )
        attempt = state.planning_review(context.repo, "attempt")
    try:
        instruction = review_instruction(attempt, git, context.repo_path)
    except (RuntimeError, subprocess.SubprocessError):
        if attempt["purpose"] != "delta":
            raise
        state.begin_planning_review(
            context.repo, purpose="full", revision=revision,
            input_fingerprint=attempt["input_fingerprint"],
            specification_fingerprint=attempt["specification_fingerprint"],
            work_cycle=work_cycle, comparison_revision=None,
        )
        attempt = state.planning_review(context.repo, "attempt")
        instruction = review_instruction(attempt, git, context.repo_path)
    if attempt["purpose"] == "final":
        validation = getattr(state, "review_validation_context", lambda _repo: "[]")(context.repo)
        instruction += ("\n저장된 Merge 작업의 검증 identity (새 목표 입력이 아님): " + validation
            + "\nnull은 저장 근거 없음이다. 이 identity를 현재 원격 구현·테스트와 대조하고, "
              "오래되거나 불완전한 근거를 최신 검증 성공으로 합성하지 마라.")
    return instruction, attempt
