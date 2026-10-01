---
id: G-097
title: Provider Auto-Review Policy Enforcement
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-088
complexity: high
risk: critical
priority: critical
---

## Purpose

G-088 establishes verifiable Agent write isolation, but R-023 also requires Codex and Claude to retain automatic approval review within that boundary. Current Codex calls use `--approve-for-me` for writing roles and Claude calls use `acceptEdits`; neither call proves that the required review policy is active. Enforce the architecture-defined review policy for every role and invocation without expanding workspace permissions.

## Acceptance Criteria

- New and resumed Planner, maintenance Planner, Worker and Reviewer calls use the G-088 verified workspace policy together with an effective provider auto-review policy. The selected worktree, role, provider, CLI version, model/account eligibility, merged settings and invocation identity are checked for each call; a command-line flag alone is not proof that review remains active.
- Codex uses `approvals_reviewer="auto_review"` with granular approval policy that rejects sandbox escalation and `request_permissions`, while preserving review of other eligible approval categories. `--approve-for-me`, `approval_policy="never"`, inherited configuration or a resume path must not silently replace that policy. Reviewer remains read-only.
- Codex calls use direct `codex exec` with the selected workspace and native role policy. Whole-process sandbox wrappers, nested sandbox compatibility probes and app-server effective-thread field confirmation are not required. Validate the supported configuration, precedence and native tool policy in the scope defined by R-023; normal Codex session/log storage and the existing provider temporary-ref exception are distinct from Agent workspace write permissions.
- Claude uses supported `auto` permission mode rather than `acceptEdits` as a substitute for automatic review. Effective settings keep the G-088 sandbox active, reject unsandboxed reruns and write-boundary expansion, and do not blanket-approve sandboxed Bash commands. The Reviewer keeps its read-only tool restrictions.
- When provider, CLI version, model/account, operating system, merged settings, sandbox initialization or session resume cannot satisfy and confirm both policies, the Agent call stops before unreviewed work. It records a specific non-secret reason and preserves the current session, workspace, work product and retry state. Unsupported Claude native Windows does not fall back to a weaker mode or new infrastructure.
- Provider auto-review is separate from Loop Manager's read-only Reviewer after Worker verification. The policy does not claim that every action inside the sandbox receives an approval review, and it does not weaken existing role-specific file-diff, Git scope, publication or PR ownership checks.

## Implementation

- Put effective approval-policy construction and validation beside G-088's provider invocation boundary, with a narrow role-independent policy result consumed by the Codex and Claude adapters. Apply it on initial calls, session resume and session-unavailable fallback; check the installed CLI and official provider documentation for supported settings before relying on them.
- Follow the user-approved direct-exec hotfix and the updated R-023 architecture. Existing G-097 app-server/envelope work must be reconciled with that implementation rather than restored on resume. Do not require a separate effective-policy output field that exec does not provide.
- Replace the convenience Codex and Claude modes in `loop_manager.py` with explicit settings that preserve automatic review and G-088's write restrictions. Reject inherited/profile/session overrides or unsupported modes when the effective policy cannot be confirmed. Keep any policy evidence bound to G-088's per-call isolation record without storing secrets or source checkout contents.
- Handle configuration rejection, classifier unavailability and denied out-of-scope actions through the existing safe Agent failure boundary. Do not retry with broader write access, disabled review, `acceptEdits` or unsandboxed execution. Keep the existing Worker-result Reviewer stage unchanged.

## Validation

- Worker runs the directly affected provider-argument and policy targeted unit tests in `tests/test_agent_invocation.py` and `tests/test_loop_manager.py` only.
- Cover Codex and Claude initial, resumed and fallback calls for writing roles and Reviewer; eligible automatic review with forbidden escalation, unsupported CLI/model/account/OS or conflicting inherited settings, review failure, and preservation of state without weaker-mode retry. Assert that review and workspace isolation are both effective, while in-sandbox actions are not incorrectly represented as universally reviewed.
- Full verify must preserve G-088 isolation, existing Git-scope safety, Agent session recovery, Worker targeted-test ownership and Loop Manager's independent Reviewer and publication gates.

## Progress

- [ ] Effective provider auto-review policy and per-call validation
- [ ] Fail-closed invocation and resume behavior
- [ ] Focused provider-policy regression tests

## Decision Log

- R-023 and `docs/ARCHITECTURE.md` already specify automatic approval review alongside workspace isolation. This plan covers that separate missing contract after G-088; it does not change the approved architecture or grant Agent Git writes.
- 2026-10-01: The user approved direct codex exec with native workspace restrictions and requested an immediate hotfix to remove the nested sandbox blocker. The direct-exec decision supersedes the pending nested-runtime restoration proposal. Preserve existing G-097 sessions, worktree and audit history; reconcile the old transport implementation with the hotfix and complete any remaining provider policy work through the normal verification and Review gates.
- 2026-10-01: The direct-exec hotfix was implemented in commit 06862bf and 39 directly affected unit cases passed. The manually executed hotfix plan is excluded from the published catalog so it cannot create a duplicate READY job. This existing plan retains its identity, sessions and execution history; Manager full verification and independent Review remain required.
