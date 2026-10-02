---
id: G-113
title: Preserve Planner Scope and Codex Configuration Boundaries
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-127
complexity: medium
risk: high
priority: critical
---

## Purpose

활성 계획의 호환 투영이 진행 중 Planner의 worktree와 snapshot을 덮어써 서로 다른 작업 공간을 비교하는 오탐 차단을 수정한다. 명시적인 별도 CODEX_HOME을 사용해도 repository 밖 사용자 설정을 project config로 다시 읽는 결함을 수정한다.

## Acceptance Criteria

- Planner가 실행 슬롯을 소유하는 동안 자동 active-plan hydration은 Planner 상태를 변경하지 않는다.
- Planner가 없으면 기존 활성 계획의 투영과 재개가 유지된다. READY priority와 dependency 선택 규칙은 바꾸지 않는다.
- Codex 사용자 설정은 선택된 CODEX_HOME에서 읽고 project config는 선택 worktree에서 repository root까지 읽는다. repository 밖 다른 사용자 설정은 project config로 읽지 않는다.
- 실제 활성 설정의 확장·exec-policy 충돌과 격리 실패는 계속 거부한다.
- 운영 복구는 DB 백업과 process lock 아래 현재 소유 workspace 및 사용자 reject 결정을 보존한다. G-058은 critical 계획 완료 전 사용자 요청에 따른 대기 상태로 보존한다.

## Implementation

- hydrate_active_project에서 기존 planning ownership 판정을 적용한다.
- Codex config 경로 수집을 .git repository 경계로 제한한다.
- 별도 운영 Codex home을 사용해 개인 plugin 설정을 유지하면서 자동 실행의 설정 충돌을 해결한다.
- 현재 Codex CLI의 `sandbox --permission-profile` 인자에 기존 최소 쓰기 경계를 명시한다. outer profile과 기존 mount gate를 함께 검증하며 inner model 명령의 network 제한과 자동 승인 검토는 유지한다.

## Validation

- Planner ownership과 기존 active-plan hydration 및 실제 config 충돌에 대한 targeted unit test만 실행한다.
- 전체 verify는 Manager가 수행한다. 운영 로그에서 scope 비교 오류 없이 Planner 또는 critical Worker가 실제 시작하는지 확인한다.

## Progress

- [x] 코드 수정 및 targeted unit test 5개 통과
- [ ] 운영 기준선 복구 및 critical 계획 선택 확인
- [ ] Manager 전체 verify, Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 사용자가 재차 차단된 자동 실행의 복구와 G-058 대신 critical 계획 처리를 요청했다. 직전 수동 해제의 IMPLEMENTING 상태가 active 재개를 유발했고, Planner snapshot과 G-058 worktree 비교 오류를 확인했다. 제품의 비선점 정책은 변경하지 않고 사용자 요청으로 G-058을 보류한다.
- OpenAI 공식 문서에 따라 Codex 사용자 설정과 repository project 설정의 경계를 구분한다. 사용자 plugin 설정은 삭제하지 않으며 새 자동 실행 home은 비밀값을 출력하거나 복제하지 않는다.

- 2026-10-01: 실제 자동 실행에서 CLI 0.159.3의 outer sandbox는 named permission profile을 요구함을 확인했다. 기존 mount proof와 쓰기 경계를 유지하는 형식으로 갱신한다. Ubuntu AppArmor가 vendor bwrap의 namespace 초기화를 거부하는 운영 환경 조건은 개별 binary profile로 해결하며 시스템 전체 userns 정책은 변경하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 Planner 소유권·설정 경계·거부 조건과 목표는 유지하고 실행 시 최신 application·Agent 경로와 현재 PRODUCT/R-050의 직접 Codex 실행 계약을 확인한다. 과거 운영·targeted test 기록은 보존하되 새 revision의 검증 성공으로 사용하지 않는다.
