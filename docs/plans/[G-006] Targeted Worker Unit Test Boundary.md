---
id: G-006
title: Targeted Worker Unit Test Boundary
sources:
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
depends_on:
  - G-005
complexity: medium
risk: medium
---

## Purpose

Worker가 자신의 변경에 직접 관련된 최소 범위의 unit test로 빠른 피드백을 완료하게 하고, 프로젝트 전체 검증과 읽기 전용 Review는 Loop Manager가 별도 단계에서 한 번씩 책임지도록 Agent와 실행 경계를 명확히 한다.

## Acceptance Criteria

- Worker prompt는 구현과 관련 unit test를 작성하거나 갱신한 뒤 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 unit test만 실행하도록 명시한다.
- Worker가 실행할 수 있는 범위는 개별 test case, test class, test module 또는 이에 준하는 좁은 단위이며, 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check와 `scripts/verify` 실행을 명시적으로 금지한다.
- targeted unit test가 실패하면 Worker는 같은 세션에서 원인을 수정하고 해당 테스트를 다시 실행하며, 문서·설정 전용 변경 또는 실행 가능한 관련 unit test가 없는 경우에는 전체 검증으로 확대하지 않고 사유를 결과에 남긴다.
- Worker가 정상 종료하면 targeted unit test 실행 여부와 관계없이 Loop Manager가 기존 VERIFYING 단계에서 전체 verify 스크립트를 실행한다.
- 전체 verify 실패만 같은 Worker 세션에 전달해 수정하게 하며, Worker 내부의 targeted unit test 수정·재실행은 별도 SQLite 재시도 횟수나 상태 전이를 만들지 않는다.
- Reviewer는 Worker 완료와 전체 verify 성공 후에만 호출되고, Worker가 보고한 targeted unit test 결과와 Loop Manager의 verify 결과를 전달받는다.
- Reviewer prompt는 테스트 command와 `scripts/verify`를 실행하지 않고 코드, 테스트와 verify 스크립트의 diff 및 전달된 결과만으로 테스트 누락, 회귀 위험과 검증 약화를 판단하도록 명시한다.
- Worker와 Reviewer prompt의 허용·금지 명령 경계, Worker 반환 뒤 verify 실행, verify 실패 시 같은 Worker 세션 재개, verify 성공 뒤 Reviewer 호출 및 Reviewer의 테스트 미실행 경계를 단위 테스트로 검증한다.

## Implementation

- Worker prompt를 관련 테스트 작성, 최소 targeted unit test 실행, 실패 시 같은 세션 내 수정·재실행, 테스트 불가 사유 보고와 금지된 전체 검증 명령이 구분되도록 구체화한다.
- Worker 실행을 재개 가능한 Codex 세션으로 연결하고 세션 ID와 마지막 Worker 결과를 프로젝트 상태에 호환 가능한 방식으로 저장해, 프로세스 재실행 또는 verify 실패 후에도 동일한 worktree와 Worker 문맥으로 돌아갈 수 있게 한다.
- Worker 성공 결과에서 targeted unit test command·결과 또는 실행하지 못한 사유를 보존하고, VERIFYING 성공 결과와 함께 Reviewer prompt에 전달한다. verify 성공 출력은 Worker에게 다시 전달하지 않는다.
- VERIFYING은 Worker 성공 뒤 항상 프로그램 코드가 실행하고, 실패할 때만 기존 IMPLEMENTING 복귀 경로를 통해 저장된 Worker 세션에 진단을 전달하도록 실행 순서를 정리한다.
- Reviewer prompt에 읽기 전용 검토 범위와 테스트·verify 재실행 금지를 추가하고, 프로그램이 verify 성공 전에는 Reviewer를 호출하지 않도록 테스트 가능한 단계 경계를 유지한다.
- 전체 Worker/Reviewer 재시도 예산과 완성형 ExecPlan 상태 모델, provider 확장, priority 기반 카탈로그 선택 및 Merge 방식과 무관한 worktree 정리는 후속 계획으로 남긴다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- Worker와 Reviewer prompt의 targeted/금지 명령 문구 단위 테스트
- Worker 성공·verify 실패·verify 성공별 호출 순서와 동일 Worker 세션 재개 단위 테스트
- targeted test 결과 또는 미실행 사유가 Reviewer에 전달되고 Reviewer가 테스트 command를 실행하지 않는 경계 단위 테스트
- 기존 SQLite schema에서 Worker 세션과 결과 필드를 추가하는 migration 단위 테스트

## Progress

- [ ] Worker targeted unit test 책임과 금지 명령을 prompt에 반영
- [ ] Worker 세션과 targeted test 결과 저장·재개 연결
- [ ] verify 이후 Reviewer 결과 전달과 읽기 전용 경계 강화
- [ ] prompt, 실행 순서, 세션 재개와 migration 단위 테스트 추가

## Decision Log

- 활성 Requirement 중 기존 Worker→VERIFYING→Reviewer 흐름에 국한돼 독립적으로 검증할 수 있는 R-003을 먼저 계획한다. priority 카탈로그와 Merge 방식별 worktree 정리는 각각 R-001과 R-002의 별도 계획으로 남긴다.
