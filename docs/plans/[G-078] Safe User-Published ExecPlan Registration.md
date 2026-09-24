---
id: G-078
title: Safe User-Published ExecPlan Registration
sources:
  - docs/requirements/[R-041] Safe Adoption of User-Published ExecPlans.md
depends_on:
  - G-036
  - G-076
complexity: high
risk: high
priority: critical
---

## Purpose

원격 기본 branch에 사용자가 직접 추가한 새 ExecPlan이 파일 검증을 통과해도 SQLite에 실행 row가 없어 `unknown-plan-id`로 프로젝트가 멈추는 문제를 해결한다. 기존 ID 및 실행 소유권 보호를 유지하면서 원격 신규 계획을 결정적으로 등록한다.

## Acceptance Criteria

- 미해결 일반·유지보수 Planner publication intent를 먼저 기존 절차로 복구한다. 해당 intent와 무관한 신규 ID를 외부 게시로 분류하지 않는다. 이후 원격 revision에 결합된 전체 active·archive catalog와 SQLite ID·소유권 snapshot을 비교한다. 기존 ID가 모두 남아 있고 신규 active ID가 저장된 최대 ID 다음부터 연속이며 경로·source·dependency graph가 유효하면, 새 ID를 미착수 READY로 정확히 한 번 등록한다.
- 신규 row의 최초 게시 경로·hash·SHA, 현재 선언 cache와 초기 Worker level은 검증한 원격 tree 내용에서 계산한다. 기본 worktree가 뒤처졌거나 로컬에 별도 변경이 있어도 이를 입력으로 사용하지 않는다.
- 새 row 등록과 기존 ID의 유효한 선언 변경 cache 갱신은 하나의 원격 revision 및 SQLite CAS 경계에서 원자적으로 적용한다. 원격 변경 또는 DB 소유권 변경으로 CAS가 실패하면 아무것도 일부 적용하지 않고 최신 상태를 다시 읽는다.
- Planner 게시 intent가 남은 채 원격 push가 끝난 경우에는 해당 manifest의 원자적 등록 경로를 재사용한다. 외부 push 뒤 DB 반영 전 중단, 같은 revision 재실행 및 이미 등록된 ID는 멱등하게 수렴한다. 외부 게시만으로 Planner 성공 지문·유지보수 완료 상태를 전진시키지 않는다.
- 기존 ID 누락과 신규 ID 동시 관측, ID gap·재사용·경로 충돌, 새 archive 파일, 유효하지 않은 catalog 또는 실행 이력에 연결된 ID는 기존 프로젝트 범위 진단을 유지하며 다른 row를 바꾸지 않는다.

## Implementation

- execplan_catalog_state.py의 원격 catalog reconciliation에 신규 active ID의 엄격한 추가 판정과 SQLite CAS 등록을 포함한다. 기존 계획 변경과 새 row 등록을 같은 transaction에서 처리하고 historical ID 및 취소 예약을 ID 경계에 포함한다.
- loop_manager.py의 Planner 게시 row 등록에서 파일시스템 의존 부분과 공통 메타데이터 구성을 분리한다. 외부 게시 수용에는 pinned Git tree의 내용과 SHA만 사용하고 기본 worktree 파일을 읽지 않는다.
- 현재 `unknown-plan-id` 진단은 위 안전 조건을 충족하지 못한 경우에 유지한다. G-077 같은 특정 ID나 commit을 예외로 하드코딩하지 않는다.

## Validation

- Worker는 tests/test_execplan_catalog_state.py와 tests/test_loop_manager.py의 새 등록·재조정 case에 대응하는 targeted unit test만 실행한다.
- 유효한 한 개·여러 개 ID, 중단 뒤 재실행, 원격 SHA drift, SQLite CAS 충돌, 뒤처진 기본 worktree와 동일 ID 재관측을 검증한다. 미해결 Planner publication intent는 원래 manifest로 복구되고 외부 게시로 중복 분류되지 않는지 확인한다.
- 기존 ID 누락과 신규 ID 동시 발생, gap·재사용·새 archive·source 오류·dependency cycle·경로 충돌은 부분 등록 없이 정확한 진단을 남기는지 검증한다.
- 전체 verify에서 Planner publication, READY 선택, archive 이력과 실행 소유권 회귀가 없어야 한다.

## Progress

- [ ] 외부 신규 active ID의 안전 판정 및 pinned Git 입력 경계 구현
- [ ] 신규 등록과 선언 cache 갱신의 원자적 CAS 처리
- [ ] 중단 복구·멱등성·안전 거부 회귀 시나리오 검증

## Decision Log

- G-077의 수동 DB 등록은 운영 복구였으며 이 계획은 같은 조작이 다시 필요하지 않도록 일반 경로를 만든다.
- 사용자 직접 게시 계획의 자동 수용은 현재 `unknown-plan-id` 거부 정책을 바꾸므로 R-041 확정과 Architecture Change Policy 검토 전에 구현하지 않는다.
