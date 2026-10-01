---
id: G-118
title: Config-Preserving Publication and Block Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-050] Direct Codex Exec with Native Sandbox.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

G-058의 저장 Agent 기준선 뒤 Manager가 다른 계획을 `push -u`로 게시하면서 shared Git config에 upstream 항목을 추가했다. 정상 게시의 부수 효과가 오래된 작업을 `shared_git_config_mutation`으로 차단한다. 불필요한 upstream 변경을 제거하고, 이미 승인된 직접 Codex 실행 결정이 반영되지 않은 G-097의 과거 차단도 현재 소유권을 확인하여 복구한다.

## Acceptance Criteria

- Manager의 일반 구현 게시와 PR 충돌 복구 push는 명시적인 origin/refspec과 기존 lease를 유지하면서 branch upstream config를 쓰지 않는다.
- 기존 Git config 보호, workspace·게시 소유권, 자동 검토 및 Reviewer 경계를 완화하지 않는다.
- 운영 G-058의 여섯 upstream 차이만 게시·Merge 기록 및 실제 설정과 대조하고 백업 후 복구한다. 다른 snapshot 차이는 기존 범위 검사로 검증하며 불확실한 차이를 일괄 허용하지 않는다.
- G-097은 R-050과 현재 계획 결정에 따라 같은 session·worktree에서 재개하도록 기존 입력 변경 복구 경로를 사용한다. 이전 nested wrapper/app-server 구현은 최신 직접 실행 구현과 조정한다.
- 운영 복구는 process lock 안에서 수행하고 원인·이전 상태·결과를 기록한다. retry·session·작업물을 보존하며 전체 verify와 Review를 통과한 것으로 기록하지 않는다.

## Implementation

- 명시적 refspec 게시에서 불필요한 `-u`를 제거한다. upstream을 소비하는 실행 경로가 없는지 확인한다.
- 게시 adapter unit test에 일반 게시·lease 게시의 config 비변경 계약을 반영한다.
- 읽기 전용 운영 진단으로 두 계획의 checkpoint와 현재 파일·Git 상태를 대조한다. SQLite backup과 작업물 보존 후 검증된 Manager 설정 차이만 조정하고 기존 재검증·차단 해제 함수를 사용한다.

## Validation

- Worker는 수정한 게시 adapter의 targeted unit test만 실행한다. 명시적 목적지·lease·실패 처리를 검증한다.
- 기존 Linux 전체 verify 스크립트는 해당 테스트를 discover한다. 전체 verify는 Manager가 실행한다.
- 운영 복구 뒤 해당 차단 제거, session/retry 보존과 실제 최신 runtime 선택을 확인한다. 새 원인이 나오면 그 근거를 별도로 검증한다.

## Progress

- [x] 운영 DB의 G-058 project block과 G-097 plan block 확인
- [x] config 비변경 게시 및 targeted unit test 4개 통과
- [ ] 백업·소유권 검증과 운영 재개
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 사용자가 이미 승인한 직접 Codex 실행 결정을 운영에 반영하고 G-058도 해결하도록 요청했다. 새로운 제품 정책을 도입하지 않고 정상 Manager 게시의 부수 효과와 오래된 차단을 복구한다.
- 수동 핫픽스 실행 기록은 `docs/plans/manual/`에 보존하여 자동 catalog에 동일 구현의 READY 작업을 중복 생성하지 않는다. 이 기록은 Merge 완료나 전체 검증 성공을 주장하지 않는다.
- targeted 첫 실행은 새 목적지 assertion이 lease 삽입 위치를 잘못 가정하여 실패했다. 명시적 목적지를 마지막 두 인자로 확인하도록 고친 뒤 일반 게시의 원격 race·lease, PR 충돌 게시와 복구 후 신규 게시 unit test 4개가 통과했다. 전체 verify 스크립트는 기존 discover로 변경 테스트를 포함하며 Worker는 실행하지 않았다.
