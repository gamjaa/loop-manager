"""Pure notification text, links and Telegram message segmentation."""
from __future__ import annotations
import re

def _checkpoint_link(origin: object, oid: object) -> str | None:
    if not origin or not oid:
        return None
    value = str(origin)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return "https://github.com/" + value + "/tree/" + str(oid)
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.removeprefix(
            "ssh://git@github.com/"
        )
    if not value.startswith("https://github.com/"):
        return None
    return value.removesuffix(".git") + "/tree/" + str(oid)


def _needs_user_attention(item: dict[str, object]) -> bool:
    # Requirement waits encode the confirmed decision in their existing reason.
    # This affects display order only; the payload and deduplication key stay intact.
    return bool(
        item.get("status") in {"DRAFT", "PR_OPEN"}
        or item.get("question") or item.get("action_targets")
        or item.get("reason_code") in {
            "execplan_catalog_invalid", "guidance_approval_required",
            "user_decision_required", "review_specification_deadlock",
            "pull_request_closed",
        }
        or (str(item.get("path", "")).startswith("docs/requirements/")
            and str(item.get("reason", "")).startswith(
                "전체 충족 판단이 불확실하여 사용자 결정을 기다립니다:"
            ))
    )


def _message(kind: str, payload: dict[str, object]) -> str:
    text = _event_message(kind, payload)
    if payload.get("occurred_at"):
        text += f"\n발생 시각: {payload['occurred_at']}"
    if payload.get("status"):
        text += f"\n처리 상태: {payload['status']}"
    return text


def _event_message(kind: str, payload: dict[str, object]) -> str:
    if kind in {"work_started", "stage_failed", "planning_result", "plans_published", "draft_decision",
                "maintenance_started", "maintenance_completed"}:
        labels = {"work_started": "작업 시작", "stage_failed": "단계 실패",
                  "planning_result": "계획 작성 결과", "plans_published": "새 계획 게시",
                  "draft_decision": "계획 게시 · 사용자 판단 대기",
                  "maintenance_started": "유지보수 시작", "maintenance_completed": "유지보수 종료"}
        lines = [f"[{payload['project']}] {labels[kind]}"]
        if payload.get("execplan_id"):
            lines.append(f"{payload['execplan_id']}: {payload.get('execplan_title') or ''}")
        for field in ("operation", "cause", "question", "url"):
            if payload.get(field):
                lines.append(str(payload[field]))
        for plan in payload.get("plans", []):
            lines.append(f"{plan['id']}: {plan['title']}")
        return "\n".join(lines)
    if kind == "resumed":
        subject = payload.get("execplan_id") or "project"
        return (
            f"[{payload['project']}] 작업 재개 ({payload['scope']}: {subject})\n"
            f"이전 차단 원인: {payload['reason_code']}\n{payload['cause']}\n"
            f"시작한 작업: {payload['operation']}"
        )
    if kind == "work_waiting":
        lines = [f"[{payload['project']}] 다음 작업 대기 (시작 가능한 작업 없음)"]
        entries = [*payload["plans"], *payload["requirements"]]
        if payload.get("project_block"):
            entries.append({
                "id": "PROJECT_BLOCKED", "reason": payload["project_block"]["reason"],
                "reason_code": payload["project_block"].get("code"),
            })
        if payload.get("planning_error"):
            entries.append({"id": "planning_error", "reason": str(payload["planning_error"])})
        plans = sorted(
            entries,
            key=lambda item: not _needs_user_attention(item),
        )
        has_actions = any(_needs_user_attention(item) for item in plans)
        section = None
        for plan in plans:
            next_section = "사용자 확인 대기:" if _needs_user_attention(plan) else "기타 대기:"
            if has_actions and section != next_section:
                lines.append(next_section)
                section = next_section
            title = " " + str(plan["title"]) if plan.get("title") else ""
            lines.append(f"- {plan['id']}{title}: {plan['reason']}")
            if plan.get("evidence") and str(plan["evidence"]) not in str(plan["reason"]):
                lines.append("  원인: " + str(plan["evidence"]))
            if plan.get("question"):
                from loop_manager_internal.catalog.execplan_drafts import fields
                lines.append("  " + fields(str(plan["question"])).get("Question", str(plan["question"])))
            if plan.get("url") or plan.get("pr_url"):
                lines.append("  " + str(plan.get("url") or plan["pr_url"]))
            if plan.get("path") and str(plan["path"]) not in str(plan["reason"]):
                lines.append("  확인 대상: " + str(plan["path"]))
            if plan.get("revision"):
                lines.append("  Revision: " + str(plan["revision"]))
            targets = [str(target) for target in plan.get("action_targets", [])
                       if str(target) not in str(plan["reason"])]
            if targets:
                lines.append("  사용자 확인 대상: " + ", ".join(targets))
        return "\n".join(lines)
    if kind == "pr_created":
        return (
            f"[{payload['project']}] {payload['execplan_id']} PR 생성\n"
            f"{payload['execplan_title']}\n"
            f"{payload['pr_url']}\n상태: PR 검토·Merge 대기"
        )
    if kind == "blocked":
        subject = payload.get("execplan_id") or payload["stage"]
        if payload.get("execplan_title"):
            subject += ": " + str(payload["execplan_title"])
        paths = "\n".join(f"- {path}" for path in payload["paths"])
        recovery = payload.get("recovery")
        action = ""
        if payload.get("specification_deadlock") or payload.get("worker_decision"):
            action = "사용자 조치: 아래 질문의 결정을 관련 문서에 반영해 주세요.\n"
        elif payload.get("guidance_locations"):
            action = "사용자 조치: 지침 제안과 User Decision을 확인해 주세요.\n"
        elif isinstance(recovery, dict) and recovery.get("user_targets"):
            action = "사용자 조치: 아래 확인 대상의 보고된 원인을 점검해 주세요.\n"
        elif payload.get("reason_code") == "pull_request_closed":
            action = "사용자 조치: Merge 없이 닫힌 같은 PR을 다시 열어 주세요.\n"
        recovery_text = ""
        if isinstance(recovery, dict):
            if recovery.get("recovery_disposition"):
                recovery_text = "\nRecovery status: " + str(recovery["recovery_disposition"])
            if recovery.get("unresolved"):
                recovery_text += "\nUnresolved: " + str(recovery["unresolved"])
            counts = recovery.get("recovery_attempts")
            if isinstance(counts, dict) and counts:
                attempts = ", ".join(
                    f"{reason}: {count}" for reason, count in sorted(counts.items())
                )
                recovery_text += f"\nRecovery attempts: {attempts}"
            stable_revision = recovery.get("last_stable_revision")
            if stable_revision:
                recovery_text += f"\nLast stable revision: {stable_revision}"
        checkpoint = payload.get("checkpoint")
        checkpoint_text = ""
        if isinstance(checkpoint, dict):
            if checkpoint.get("result") == "published":
                checkpoint_text = (
                    "\nCheckpoint: "
                    + str(checkpoint.get("branch_url") or checkpoint.get("branch"))
                    + "\nCommit: "
                    + str(checkpoint.get("commit_oid"))
                )
                if checkpoint.get('diagnostic'):
                    checkpoint_text += '\nDiagnostic ref: ' + str(checkpoint.get('branch'))
                    checkpoint_text += '\nOriginal branch: ' + str(checkpoint.get('original_branch'))
            else:
                checkpoint_text = "\nCheckpoint: local only (" + str(
                    checkpoint.get("reason")
                ) + ")"
                if checkpoint.get('worktree'):
                    checkpoint_text += '\nPreserved worktree: ' + str(checkpoint['worktree'])
                if checkpoint.get('diagnostic'):
                    checkpoint_text += '\nOriginal branch: ' + str(checkpoint.get('original_branch'))
            if checkpoint.get('diagnostic'):
                if checkpoint.get('complete') is False:
                    checkpoint_text += '\nIncomplete artifacts: ' + str(checkpoint.get('omitted'))
                if checkpoint.get('original_unpublished_reason'):
                    checkpoint_text += '\nOriginal publication: ' + str(checkpoint['original_unpublished_reason'])
        return (
            f"[{payload['project']}] BLOCKED\n"
            + action
            + f"작업: {payload.get('scope')}: {subject}\n"
            + f"Reason: {payload.get('reason_code')}\n원인: {payload['cause']}"
            + (f"\n확인 대상:\n{paths}" if paths else "")
            + (f"\nQuestion: {payload['specification_deadlock']['question']}"
               if payload.get('specification_deadlock') else "")
            + (f"\nQuestion: {payload['worker_decision']['question']}"
               if payload.get('worker_decision') else "")
            + recovery_text
            + checkpoint_text
        )
    if kind == "persistent_execution_failure":
        subject = payload.get("execplan_id") or "project"
        attempts = payload.get("recovery_attempts")
        attempt_text = ""
        if isinstance(attempts, dict) and attempts:
            attempt_text = "\nRecovery attempts: " + ", ".join(
                f"{reason}: {count}" for reason, count in sorted(attempts.items())
            )
        return (
            f"[{payload['project']}] Persistent execution failure "
            f"({payload['stage']}: {subject})\n"
            f"확인 요청: {payload['required_action']}\n"
            f"Reason: {payload['reason_code']}\n{payload['cause']}"
            f"\nConsecutive external runs: {payload['consecutive_external_runs']}"
            + attempt_text
        )
    return (
        f"[{payload['project']}] Project complete\n"
        f"{payload.get('reason', 'Planner confirmed no additional work.')}\n"
        "Last completed work: "
        f"{payload.get('last_completed_plan_id') or '-'} "
        f"{payload['last_completed_title']}"
    )


MESSAGE_LIMIT = 4096


def _pieces(text: str) -> list[str]:
    return [text[start:start + MESSAGE_LIMIT]
            for start in range(0, len(text), MESSAGE_LIMIT)]
