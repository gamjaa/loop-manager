---
id: G-243
title: PR 전 개정 snapshot의 수동 소유 보호
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-077] Manual Plan Work Start and Completion.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

PR 전 일반 계획 개정에 자동 작업의 실행 이력을 허용하면서 R-077의 수동 소유 계획·준비 intent 보호를 유지한다. 현재 mutable snapshot은 수락 뒤 IMPLEMENTING·VERIFYING·REVIEWING인 수동 구성원도 포함할 수 있고, 게시 예약은 기존 G의 수동 membership을 확인하지 않는다.

## Acceptance Criteria

- 일반 mutable snapshot은 수동 소유 계획과 start/submit 준비 intent의 대상 G를 제외한다. complete/submit 수락 뒤에도 묶음 identity·실행 이력·자원 소유가 남은 계획은 자동 개정·rename·삭제 대상이 아니다. compact catalog의 상태·관계와 사용자 직접 개정 반영은 유지한다.
- snapshot 생성 뒤 수동 시작·등록·묶음 membership이 바뀌면 기존 transaction·소유 예약의 게시 직전 검사에서 해당 자동 편집을 거부한다. exec_plans row가 그대로여도 별도 수동 intent·membership 변경을 놓치지 않으며, 독립 계획 게시를 불필요하게 막지 않는다.
- 실제 종료·정당한 수동 대상 해제 뒤 과거 membership/감사만 남은 대상은 현재 수동 소유로 추정하지 않는다. 소유권 해제 근거가 없으면 기존 보호를 유지하고 제출 취소만으로 자동 개정 대상에 넣지 않는다.
- 자동 작업은 실행 이력이 있는 READY·진행 상태와 branch·worktree·session·checkpoint만으로 제외되지 않는다. R-082의 실제 PR 조회·동일 ID·시작한 경로와 이력 보존을 유지하며 유지보수·코드 점검의 무소유 READY 범위를 넓히지 않는다.
- 단계별 문서 영향 판단도 수동 소유권을 개정 권한으로 바꾸지 않는다. 최신 조건의 구현·검증·Review 판단과 기존 자동 후속 교정은 유지하고 허용되지 않은 계획 수정안은 게시하지 않는다. 사용자 작업·생략 원문·실패와 준비 intent를 보존한다.
- PRODUCT·ARCHITECTURE의 PR 전 개정 설명에 R-077의 수동 소유 예외를 반영한다. 자동 교정 허용과 Planner 편집 권한을 구분하며 새로운 수동 인수 정책을 만들지 않는다.

## Implementation

`storage/manual_work_state.py`·`work_claims.py`의 기존 묶음/준비 소유 근거를 일반 snapshot 선정과 게시 직전 검사에서 재사용한다. 별도 수동 저장 상태를 row의 실행 상태와 함께 확인하고, 단계별 단일 계획 개정에도 같은 권한 경계를 적용한다. 기존 예약 transaction·CAS·사용자 변경 reconciliation을 유지하며 소유 계획을 무소유로 되돌리거나 보호를 상태 이름만으로 판정하지 않는다.

## Validation

- `tests/test_manual_work.py`·`tests/test_manual_concurrency.py`·`tests/test_incremental_planning.py`의 최소 targeted case로 수동 대기/준비 intent, 수락 뒤 각 실행 상태·복수 G membership, snapshot 이후 시작/등록과 독립 자동 계획의 개정을 검사한다. 실제 SQLite membership·reservation을 사용하고 Git/PR 관측은 기존 mock 경계로 대체한다.
- 단계별 문서 판단에서 수동 대상 수정안 무게시와 필요한 자동 교정 유지, 사용자 직접 개정·생략 무효화·session/작업물 보존을 확인한다. 기존 자동 started READY 개정과 실제 PR 출현 거부는 계속 통과해야 한다.
- Manager 전체 verify는 수동 준비 병행·묶음 원자성·재시작 intent·일반/유지보수 snapshot 및 게시 안전 회귀를 유지한다. Reviewer는 권한 확대·수동 원문 또는 작업물 손실 없이 최신 문서 조건을 처리하는지 읽기 전용으로 확인한다.

## Progress

- [x] eb797d3a에서 mutable snapshot의 상태 확대와 `allow_started=True`, 수동 membership·게시 예약 검사를 정적으로 대조했다. R-077의 명시적 보호는 R-082의 Non-Goals로 폐지되지 않았고 기존 수동 경합 테스트는 수락 뒤 새 snapshot 포함을 검사하지 않는다. 구현·테스트 실행 없음.
- [ ] 수동 소유 선정/게시 보호와 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 수동 membership·intent와 snapshot/CAS의 책임 경계가 명확한 권한 검사 보완이다. risk high: 사용자 수동 계획의 자동 개정·삭제나 묶음 원문 훼손은 원격 문서와 수동 작업의 복구를 요구한다. 수락 뒤 보호·경합 거부·독립 작업 지속과 이력 보존을 검증한다.
- R-082는 자동 작업의 실행 이력만을 이유로 개정을 금지하지 않도록 변경한다. R-077의 수동 소유 예외와 사용자 직접 개정은 그대로 적용하며 직접 구현 전체를 소급 계획하지 않는다.
