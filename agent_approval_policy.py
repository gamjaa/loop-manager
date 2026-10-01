"""Provider approval policy, independent of Manager's post-Worker Reviewer.

Only eligible approval requests receive provider review. In-sandbox actions
are not universally reviewed. Policy confirmation never grants more access.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re


class ApprovalPolicyError(RuntimeError):
    """Automatic review could not be confirmed; never retry in a weaker mode."""


def granular_policy() -> dict:
    return {"granular": {"sandbox_approval": False, "request_permissions": False,
                         "rules": True, "mcp_elicitations": True, "skill_approval": True}}


def codex_configuration(read_only: bool) -> tuple[str, ...]:
    mode = "read-only" if read_only else "workspace-write"
    return (
        "-c", f'sandbox_mode="{mode}"',
        "-c", 'approvals_reviewer="auto_review"',
        "-c", "approval_policy={granular={sandbox_approval=false,request_permissions=false,"
        "rules=true,mcp_elicitations=true,skill_approval=true}}",
        "-c", "sandbox_workspace_write.writable_roots=[]",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "mcp_servers={}",
    )


def claude_arguments(read_only: bool, protected: tuple[str, ...] = ()) -> list[str]:
    arguments = ["--permission-mode", "auto"]
    if read_only:
        arguments.extend(["--disallowedTools", "Edit", "Write", "NotebookEdit"])
    settings = {"permissions": {"defaultMode": "auto"}, "sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": False, "excludedCommands": [],
        "filesystem": {"denyWrite": list(protected)}}}
    arguments.extend(["--settings", json.dumps(settings, sort_keys=True)])
    return arguments


def supported_codex(version: str) -> None:
    # 0.159.3 is the installed, schema-checked protocol baseline. Newer servers
    # still must confirm every field below; version alone is never evidence.
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (0, 159, 3):
        raise ApprovalPolicyError("Codex CLI가 검증 가능한 자동 검토 프로토콜을 지원하지 않습니다.")


@dataclass(frozen=True)
class ReviewConfirmation:
    digest: str
    session_id: str


def _require_policy(policy: object, reviewer: object) -> None:
    expected = granular_policy()
    if (not isinstance(policy, dict) or policy != expected
            or not isinstance(policy.get("granular"), dict)
            or any(type(value) is not bool for value in policy["granular"].values())
            or reviewer != "auto_review"):
        raise ApprovalPolicyError("Codex 유효 승인 정책이 자동 검토·권한 확대 거부 정책과 다릅니다.")


def confirm_codex(config: dict, session: dict, account: dict, *,
                  model: str, effort: str, worktree: Path, temporary: Path,
                  read_only: bool, session_id: str | None) -> ReviewConfirmation:
    """Validate merged config and the actual thread before sending any work."""
    _require_policy(config.get("approval_policy"), config.get("approvals_reviewer"))
    _require_policy(session.get("approvalPolicy"), session.get("approvalsReviewer"))
    principal = account.get("account")
    if (account.get("requiresOpenaiAuth") is not True or not isinstance(principal, dict)
            or principal.get("type") not in ("chatgpt", "apiKey")):
        raise ApprovalPolicyError("Codex 자동 검토를 위한 계정 인증·지원 여부를 확인할 수 없습니다.")
    if (session.get("model") != model or session.get("modelProvider") != "openai"
            or session.get("reasoningEffort") != effort):
        raise ApprovalPolicyError("Codex 유효 모델·provider·effort가 선택한 호출과 다릅니다.")
    thread = session.get("thread")
    active = thread.get("id") if isinstance(thread, dict) else None
    if (not isinstance(active, str) or not active or (session_id and active != session_id)
            or session.get("cwd") != str(worktree)
            or (isinstance(thread, dict) and thread.get("cwd") != str(worktree))):
        raise ApprovalPolicyError("Codex 유효 session·worktree identity가 선택한 호출과 다릅니다.")
    sandbox = session.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("networkAccess") is not False:
        raise ApprovalPolicyError("Codex 유효 sandbox의 네트워크 경계를 확인할 수 없습니다.")
    if read_only:
        if sandbox != {"type": "readOnly", "networkAccess": False}:
            raise ApprovalPolicyError("Codex Reviewer의 유효 sandbox가 read-only가 아닙니다.")
    else:
        roots = sandbox.get("writableRoots")
        if (sandbox.get("type") != "workspaceWrite"
                or sandbox.get("excludeSlashTmp") is not True
                or sandbox.get("excludeTmpdirEnvVar") is not True
                or not isinstance(roots, list)
                or any(not isinstance(root, str) for root in roots)
                or set(roots) not in ({str(temporary)}, {str(temporary), str(worktree)})
                or any(not isinstance(root, str) or str(Path(root).resolve()) != root
                       for root in roots)
                or set(sandbox) != {"type", "networkAccess", "writableRoots",
                                     "excludeSlashTmp", "excludeTmpdirEnvVar"}):
            raise ApprovalPolicyError("Codex 유효 sandbox의 쓰기 경계가 확대됐거나 확인되지 않았습니다.")
    if session.get("runtimeWorkspaceRoots", []) not in ([], [str(worktree)]):
        raise ApprovalPolicyError("Codex session에 추가 workspace 쓰기 경계가 있습니다.")
    profile = session.get("activePermissionProfile")
    if profile is not None and (not isinstance(profile, dict)
            or profile.get("id") != (":read-only" if read_only else ":workspace")
            or profile.get("extends") is not None):
        raise ApprovalPolicyError("Codex session이 검증하지 않은 permission profile을 사용합니다.")
    # Store only a digest, never account identifiers, configuration secrets or
    # session history. The caller binds this to G-088's physical invocation.
    material = {"policy": granular_policy(), "reviewer": "auto_review", "sandbox": sandbox,
                "model": model, "effort": effort, "worktree": str(worktree),
                "session": active, "account": principal}
    return ReviewConfirmation(sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(), active)
