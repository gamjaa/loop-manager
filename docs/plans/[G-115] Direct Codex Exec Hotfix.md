---
id: G-115
title: Direct Codex Exec Hotfix
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-050] Direct Codex Exec with Native Sandbox.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

Restore runnable Codex Worker and Reviewer calls by removing the whole-process sandbox wrapper. Manager must invoke codex exec directly with the existing workspace, granular auto-review and role restrictions, while checking the native tool boundary without a nested sandbox.

## Acceptance Criteria

- Initial, resumed and fallback Codex calls start with codex exec, preserve the selected canonical workspace, read-only Reviewer and workspace-write writing roles, and reject sandbox escalation and permission expansion.
- Preflight checks the native tool policy with one standalone sandbox invocation; it never wraps the provider or verifies mount namespaces of the entire process tree. Protected source, other worktrees and Git paths are checked without modifying them.
- Existing role/repository session storage, logs, usage parsing, process cleanup and atomic runtime handoff checks remain effective. Rejection preserves prior results and invocation evidence is invalidated on failure.
- Reviewer instructions continue to forbid test and verify reruns independently of filesystem read-only enforcement.
- The G-097 runtime compatibility block is resolved through the changed implementation and specification, retaining its worktree, sessions and audit history. Existing policy work is not discarded or silently marked completed.

## Implementation

- Replace the outer-envelope launch policy in agent_codex_sandbox.py with a direct provider launch and separate native-policy probe.
- Keep configuration, workspace, session and call-bound evidence validation in agent_write_isolation.py; use the same settings and environment for probing and direct exec.
- Adapt directly affected provider invocation tests, preserving timeout, handoff, session and failure coverage. Maintain the complete verification scripts.
- Reconcile the user-approved direct-exec decision with G-097 and the runtime block through supported Manager recovery; do not directly manipulate SQLite.

## Validation

- Run the affected WriteIsolationTests and RuntimeReadOnlyIsolationTests plus narrowly selected Codex argument/session/Reviewer prompt unit cases. Use mocked CLI processes for unit verification.
- Manager owns the full verify script and independent Reviewer. Worker does not run full tests, integration tests or verify.

## Progress

- [x] Direct provider launch and standalone tool-policy check
- [x] Targeted regression verification
- [ ] Hotfix publication and supported runtime recovery

## Decision Log

- 2026-10-01: The user approved direct codex exec and requested immediate implementation as a hotfix because the existing block prevents progress. Native workspace restrictions and auto-review remain required. Draft R-050 may be removed after its decisions have been implemented and preserved in the lasting specifications; no existing requirement, audit or active plan source may be lost.
- The direct launch retains the existing role/repository CODEX_HOME and per-call result paths, subprocess-group cleanup and handoff spawn gate. A standalone native tool-policy probe checks only declared protected roots and non-destructive protected-file write opens, then exercises writes in allowed scratch/workspace roots. It never executes the provider, scans unrelated mounts or wraps exec. New policy evidence is explicitly fingerprinted as native-tools-v2.
- Verification: 39 directly affected unit cases passed in 0.157 seconds. These cover native-policy confirmation/rejection, source preservation, initial/resumed role calls, evidence lifetime, session parsing, handoff, process cleanup, runtime document generation and Reviewer test restrictions. CLI/kernel processes and Git observations were mocked. Worker did not execute integration tests or the full verify script. Existing Linux and Windows verification scripts already include every affected module and their full coverage is unchanged. Manager full verification and independent Review remain outstanding; this is not a completed ExecPlan claim.
- G-097's main-branch specification now records the approved direct-exec decision. Its saved worktree, Worker/Reviewer sessions, PLAN_BLOCKED state and retry audit were inspected read-only and preserved. The normal Manager input-change reconciliation owns resumption and any required rebase/correction; no SQLite mutation or workspace reset was performed.
