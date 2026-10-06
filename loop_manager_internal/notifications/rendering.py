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


def _message(kind: str, payload: dict[str, object]) -> str:
    if kind == "resumed":
        subject = payload.get("execplan_id") or "project"
        return (
            f"[{payload['project']}] 작업 재개 ({payload['scope']}: {subject})\n"
            f"이전 차단 원인: {payload['reason_code']}\n{payload['cause']}\n"
            f"시작한 작업: {payload['operation']}"
        )
    if kind == "work_waiting":
        lines = [f"[{payload['project']}] 다음 작업 대기"]
        for plan in payload["plans"]:
            lines.append(f"- {plan['id']} {plan['title']}: {plan['reason']}")
            if plan.get("question"):
                from loop_manager_internal.catalog.execplan_drafts import fields
                lines.append("  " + fields(str(plan["question"])).get("Question", str(plan["question"])))
            if plan.get("url"):
                lines.append("  " + str(plan["url"]))
            if plan.get("revision"):
                lines.append("  Revision: " + str(plan["revision"]))
            if plan.get("action_targets"):
                lines.append("  사용자 확인 대상: " + ", ".join(plan["action_targets"]))
        for requirement in payload["requirements"]:
            lines.append(f"- {requirement['id']}: {requirement['reason']}")
        if payload.get("project_block"):
            lines.append("PROJECT_BLOCKED: " + payload["project_block"]["reason"])
        if payload.get("planning_error"):
            lines.append("planning_error: " + str(payload["planning_error"]))
        return "\n".join(lines)
    if kind == "pr_created":
        return (
            f"[{payload['project']}] PR created\n"
            f"{payload['execplan_id']}: {payload['execplan_title']}\n"
            f"{payload['pr_url']}"
        )
    if kind == "blocked":
        subject = payload.get("execplan_id") or payload["stage"]
        paths = "\n".join(f"- {path}" for path in payload["paths"])
        recovery = payload.get("recovery")
        recovery_text = ""
        if isinstance(recovery, dict):
            counts = recovery.get("recovery_attempts")
            if isinstance(counts, dict) and counts:
                attempts = ", ".join(
                    f"{reason}: {count}" for reason, count in sorted(counts.items())
                )
                recovery_text = f"\nRecovery attempts: {attempts}"
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
            f"[{payload['project']}] BLOCKED ({payload.get('scope')}: {subject})\n"
            f"Reason: {payload.get('reason_code')}\n{payload['cause']}"
            + recovery_text
            + (f"\nFiles to edit:\n{paths}" if paths else "")
            + (f"\nQuestion: {payload['specification_deadlock']['question']}"
               if payload.get('specification_deadlock') else "")
            + (f"\nQuestion: {payload['worker_decision']['question']}"
               if payload.get('worker_decision') else "")
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
            f"Reason: {payload['reason_code']}\n{payload['cause']}"
            f"\nConsecutive external runs: {payload['consecutive_external_runs']}"
            + attempt_text
            + f"\nAction: {payload['required_action']}"
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
