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

PR 전 일반 계획 개정에 자동 작업의 실행 이력을 허용하면서 R-077의 수동 소유 계획·준비 intent 보호를 유지한다. 직접 구현된 snapshot 생성 시 수동 membership 제외를 재사용하고, 준비 intent 및 snapshot 이후 새 membership/소유 변경을 게시 직전까지 검증하는 남은 경계를 보완한다.

## Acceptance Criteria

- 일반 mutable snapshot은 수동 소유 계획과 start/submit 준비 intent의 대상 G를 제외한다. complete/submit 수락 뒤에도 묶음 identity·실행 이력·자원 소유가 남은 계획은 자동 개정·rename·삭제 대상이 아니다. compact catalog의 상태·관계와 사용자 직접 개정 반영은 유지한다.
- snapshot 생성 뒤 수동 시작·등록·묶음 membership이 바뀌면 기존 transaction·소유 예약의 게시 직전 검사에서 해당 자동 편집을 거부한다. exec_plans row가 그대로여도 별도 수동 intent·membership 변경을 놓치지 않으며, 독립 계획 게시를 불필요하게 막지 않는다.
- 실제 종료·정당한 수동 대상 해제 뒤 과거 membership/감사만 남은 대상은 현재 수동 소유로 추정하지 않는다. 소유권 해제 근거가 없으면 기존 보호를 유지하고 제출 취소만으로 자동 개정 대상에 넣지 않는다.
- 자동 작업은 실행 이력이 있는 READY·진행 상태와 branch·worktree·session·checkpoint만으로 제외되지 않는다. R-082의 실제 PR 조회·동일 ID·시작한 경로와 이력 보존을 유지하며 유지보수·코드 점검의 무소유 READY 범위를 넓히지 않는다.
- 프로젝트 일반 planning의 개정과 이전 버전의 단일 작업 개정 후보 복구 모두 수동 소유권을 개정 권한으로 바꾸지 않는다. 새 ExecPlan 실행 대응 판단은 plan_text를 반환하지 않고 최신 조건의 구현·검증·Review와 기존 자동 후속 교정을 유지한다. 사용자 작업·생략 원문·실패·준비 intent와 보존된 개정 후보를 무단 게시·폐기하지 않는다.
- PRODUCT·ARCHITECTURE의 PR 전 개정 설명에 R-077의 수동 소유 예외를 반영한다. 자동 교정 허용과 Planner 편집 권한을 구분하며 새로운 수동 인수 정책을 만들지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/planning/planner_validation.py`의 `mutable_ready_snapshot()`은 살아 있는 `manual_work_members`·`manual_work_groups`를 조회해 제외한다. 이 직접 구현을 유지하며 구성원 등록 전의 준비 intent·자원 예약과 실제 해제 근거를 `storage/manual_work_state.py`·`work_claims.py`의 기존 소유 정보로 함께 확인한다.
- [ ] `_assert_mutable_ready_snapshot_current()`와 planning publication의 writer transaction에서 exec_plans row 이외의 membership·준비/소유 변경도 재검사한다. 현재 `work_claims.assert_manual_scopes_available()`의 Requirement 경로/new source 보호만으로 기존 G 개정 보호가 완료됐다고 판단하지 않는다. 독립 계획의 게시 범위는 보존한다.
- [ ] `execution/stage_documents.py`의 legacy proposed/prepared 복구에 같은 게시 권한 검사를 유지한다. 새 실행 대응은 plan_text=null 계약을 재사용하고 단일 계획 개정 생성 경로를 다시 활성화하지 않는다. R-082 일반 planning 전환 중 수동 소유 예외도 원래 작업/묶음 identity에 결합한다.
- [ ] 기존 예약 transaction·CAS·사용자 변경 reconciliation과 PRODUCT·ARCHITECTURE의 수동 소유 설명을 필요한 범위만 정합화한다. 수락된 수동 작업을 자동 개정 가능 상태로 바꾸지 않는다.

## Validation

- `tests/test_manual_work.py`·`tests/test_manual_concurrency.py`·`tests/test_incremental_planning.py`의 최소 targeted case로 수동 대기/준비 intent, 수락 뒤 각 실행 상태·복수 G membership, snapshot 이후 시작/등록과 독립 자동 계획의 개정을 검사한다. 실제 SQLite membership·reservation을 사용하고 Git/PR 관측은 기존 mock 경계로 대체한다.
- 새 ExecPlan 실행 대응의 plan_text 거부, legacy 단일 개정 후보의 수동 대상 무게시, 필요한 자동 교정 유지와 사용자 직접 개정·생략 무효화·session/작업물 보존을 확인한다. snapshot 생성 뒤 membership만 바뀌고 exec_plans 실행 row는 같은 경합도 포함한다. 기존 자동 started 개정과 실제 PR 출현 거부는 계속 통과해야 한다.
- Manager 전체 verify는 수동 준비 병행·묶음 원자성·재시작 intent·일반/유지보수 snapshot 및 게시 안전 회귀를 유지한다. Reviewer는 권한 확대·수동 원문 또는 작업물 손실 없이 최신 문서 조건을 처리하는지 읽기 전용으로 확인한다.

## Progress

- [x] eb797d3a에서 mutable snapshot의 상태 확대와 `allow_started=True`, 수동 membership·게시 예약 검사를 정적으로 대조했다. R-077의 명시적 보호는 R-082의 Non-Goals로 폐지되지 않았고 기존 수동 경합 테스트는 수락 뒤 새 snapshot 포함을 검사하지 않는다. 구현·테스트 실행 없음.
- [ ] 수동 소유 선정/게시 보호와 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-14 delta 개정: snapshot 생성의 수동 membership 제외는 직접 반영돼 재구현 대상에서 뺐다. 게시 직전 검사에는 별도 membership/준비 intent의 재확인이 남아 있으며 R-082의 일반 planning과 legacy 후보 복구에 그 보호를 연결한다. 기존 목표·분류·dependency·수행 기록을 보존하고 테스트·verify 성공을 합성하지 않는다.

- complexity medium: 기존 수동 membership·intent와 snapshot/CAS의 책임 경계가 명확한 권한 검사 보완이다. risk high: 사용자 수동 계획의 자동 개정·삭제나 묶음 원문 훼손은 원격 문서와 수동 작업의 복구를 요구한다. 수락 뒤 보호·경합 거부·독립 작업 지속과 이력 보존을 검증한다.
- R-082는 자동 작업의 실행 이력만을 이유로 개정을 금지하지 않도록 변경한다. R-077의 수동 소유 예외와 사용자 직접 개정은 그대로 적용하며 직접 구현 전체를 소급 계획하지 않는다.
