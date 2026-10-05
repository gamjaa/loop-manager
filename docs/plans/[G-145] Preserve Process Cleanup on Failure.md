---
id: G-145
title: Preserve Process Cleanup on Failure
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
depends_on:
  - G-158
complexity: medium
risk: medium
priority: normal
---

## Purpose

Keep resource cleanup independent when process or lock handling fails. Verify child tracking failure currently lets one signal error skip remaining child cleanup and hide the original failure; process-lock release error can skip closing the handle. Related tests do not reliably exercise Windows lock/verify paths or clean up a looping child after assertion failure.

## Acceptance Criteria

- On verify failure or timeout, every known child/group cleanup is attempted independently and the original verification failure plus any uncertain termination remains observable. No result is reported as passed while a child may still be running.
- Process lock handle closes even if unlock fails. Existing lock exclusivity, timeout policy, verify exit codes and platform-specific commands remain unchanged.
- Tests release their spawned children on all assertion paths, and Windows cases actually reach the intended lock and verify handoff behavior.

## Implementation

- Keep cleanup in the existing verify-process and process-lock modules; collect cleanup errors without replacing the primary failure. Use the platform's current lock implementation in test doubles.
- The responsibility boundary is primary process failure reporting versus independent child and lock cleanup effects. Preserve user behavior, public contract and storage format by retaining verify exit results and lock ownership while ensuring every known cleanup is attempted.

## Validation

- Worker runs targeted unit tests in `tests/test_verify_process.py`, `tests/test_execution_progress_logs.py`, `tests/test_registered_project_scheduler.py` and `tests/test_verify_timeout_policy.py` with injected signal/unlock failures and two independent plans.
- Full verify confirms timeout, lock, scheduler handoff and no-child-leak behavior on supported platforms.

## Progress

- [ ] Make cleanup attempts independent and preserve primary failure.
- [ ] Strengthen process, lock and cross-platform regression cases.

## Decision Log

- Manifest findings 9 and 34 identify resource-finalization gaps; findings 49, 77, 84 and 85 identify tests that can miss them. The plan does not change Worker/Manager verification responsibility.
- Observed process paths: `loop_manager_internal/execution/verify_process.py`, `loop_manager_internal/runtime/process_lock.py`.

- 2026-10-05 사용자 실행 순서 결정: G-158의 archive Requirement 감사·복구 준비 PR이 실제 Merge된 뒤 기존 세션과 후보를 보존하여 재개한다. 미충족 Requirement는 활성 입력으로 유지하고 조기 archive 강제를 완료 조건으로 적용하지 않는다. 재개 후 targeted test, Manager 전체 verify, Reviewer와 PR 게시를 통해 실제 복구를 확인하며 다른 안전 차단은 별도로 판단한다.
