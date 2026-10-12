---
id: G-245
title: Planner 종료와 교정 경계의 원격 입력 갱신
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

Planner 작성·교정 종료 뒤 다음 판단에 들어가기 전에 최신 원격 명세와 ExecPlan을 반영한다. 현재 lifecycle은 진입 시 갱신한 뒤 Agent 결과를 즉시 검증하고, 일반 결과의 원격 전진은 주로 게시 경계에서 발견한다. 충돌 교정도 저장된 target으로 진행한다. 게시 직전 거부만으로 R-082의 종료·교정 경계 갱신과 최신 입력에 따른 재평가를 충족하지 못한다.

기존 workspace rebase·문서 관련성·결과 보존·게시 직전 검사를 재사용해 단계 순서를 보완한다. 직접 구현된 Worker/verify/Reviewer 문서 반영이나 일반 작업 선택 정책은 다시 구현하지 않는다.

## Acceptance Criteria

- 일반 Planner와 쓰기를 수행하는 유지보수 Planner의 진입·종료·교정·게시 전 안전한 경계에서 최신 원격 원본을 확인한다. Agent 또는 다른 writer 실행 중 동일 worktree의 pull/rebase를 겹치지 않고, 실행 중 바뀐 원격은 writer 종료 후 다음 의미 검증·교정·게시 전에 반영한다. mode와 제공된 개정 권한은 유지한다.
- 작성 결과 검증과 검증 실패 교정은 최신 명세·ExecPlan을 입력으로 사용한다. 원격 관련 입력이 달라졌으면 이전 입력의 결과를 확정·게시하거나 그 결과의 형식/내용 실패만으로 교정 횟수·planning 오류를 먼저 확정하지 않는다. 이전에 유효하게 기록한 독립 실패·실행 이력은 초기화하지 않는다.
- 기존 rebase/journal 경계로 candidate의 동일 ID·staged/unstaged/untracked 작업물·세션과 완료/미완료 결과를 보존한다. 현재 원격 기준과 권한 snapshot을 다시 확인하며, 작업물을 다시 구성하거나 시작한 계획의 경로·기존 수행 기록을 잃지 않는다.
- 실제 변경이 현재 mode의 목표·범위·제약·완료 조건에 미치는 영향을 대조한다. 연결 Requirement와 적용 상위 명세의 변경은 ExecPlan 파일이 같거나 `sources`에 직접 나열되지 않았어도 반영한다. 관련 입력이 바뀌었을 때만 영향을 받은 판단·수정안을 최신 입력으로 재평가하고 필요한 교정부터 같은 세션으로 이어간다.
- 무관한 문서 변경은 rebase 후 이미 완료한 결과와 하던 단계를 유지하고 Planner를 반복 호출하지 않는다. 문서/코드가 함께 바뀌면 기존 후보·입력 검증을 적용한다. 원격 관측·rebase 성공 자체를 의미 검토나 새 조건의 검증 성공으로 기록하지 않는다.
- 충돌 교정 진입과 교정 writer 종료 뒤의 원격 전진도 처리한다. 진행 중 rebase/stash 교정은 기존 journal의 target·소유권을 유지해 안전하게 마친 뒤 필요한 최신 기준 반영으로 연결하고 중첩 rebase를 시작하지 않는다. 저장된 교정 결과는 현재 입력과 연결해 재사용 여부를 판단하며, 오래된 target의 교정만으로 최신 결과를 확정하지 않는다.
- 원격 확인 실패·관측 경쟁·문서 부재·관련성 미확인·충돌은 현재 작업과 원인을 보존하고 기존 재시도/복구/판단 대기로 연결한다. 반영 중 중단 뒤에는 저장된 원격 기준·남은 교정과 작업물을 대조해 재개하며, 완료한 반영을 중복 수행하거나 미완료 교정을 건너뛰지 않는다. 게시 직전 CAS·PR·소유권 검사를 유지한다.
- PRODUCT·ARCHITECTURE의 Planner 단계 경계 설명을 실제 동작과 정합하게 갱신한다. Worker/verify/Reviewer 단계 판단·일반 priority·유지보수 평가 횟수와 완료 정책·빠른 점검의 읽기 전용 권한은 변경하지 않는다.

## Implementation

`planning/planning_runtime.py`의 단계 dispatch, `workspace_service.py`의 기존 원격 갱신·관련성·result 보존과 `assessment_service.py`의 검증/교정 진입을 연결한다. 최신 관측과 반영 완료가 다음 판단의 입력이 되도록 결과 유효성과 재개 상태를 구분한다. 기존 fingerprint·허용 snapshot·세션·rebase journal과 publication의 재확인을 재사용하고 별도 병렬 writer나 독립적인 재시도 상태를 추가하지 않는다.

`planning/conflict_correction.py`의 교정 호출과 저장 결과 재개에도 같은 원격 입력 경계를 연결한다. 교정 중인 Git 작업의 완료와 최신 원격 기준 반영을 분리해 기존 보존 절차를 지킨다. 관련성이 있는 변경만 현재 판단을 다시 수행하며 안전하게 확정할 수 없으면 결과와 원인을 보존한다.

Coordinator는 성공한 Planner 결과를 `_validate_planner_result`에 넘기기 전에 `PlanningWorkspaceService`의 안전 경계 갱신을 다시 실행한다. 관련 입력 변경으로 완료 결과가 pending 재평가로 바뀌면 이전 결과 검증을 건너뛰고 기존 ID·작업물·session으로 Planner 단계에 돌아간다. 새 입력 결과는 이전 결과의 검증 교정 횟수를 이어받지 않지만 과거 진단과 독립 오류·실행 이력은 보존하며, 연결된 오류의 correction binding이 유효하면 해결될 때까지 새 입력의 제한된 교정을 재개한다. 충돌 교정은 journal의 원래 target으로 기존 Git operation을 마치고 상태·journal을 확정한 뒤 원격을 재관측하며, 더 새 target이 있으면 다음 workspace 경계까지 연기한다.

## Validation

- Worker는 `tests/test_planning_assessment.py`, `tests/test_planner_validation_recovery.py`와 planning lifecycle fixture의 최소 targeted unit case로 작성 중 원격 Requirement/상위 명세 변경, 종료 직후 전진, 실패 결과 검증 전 전진과 교정 호출 전 변경을 검사한다. 실제 coordinator/handler의 호출 순서·전달된 원격 입력·같은 session·오류 횟수와 게시 여부를 확인한다.
- 무관한 문서 변경 때 완료 결과 무재호출, 관련 변경 때 필요한 판단만 재수행, 문서/코드 혼합, 관측 실패·반영 후 재시작을 검사한다. 일반·유지보수 mode의 목표/권한 보존과 기존 독립 실패 이력·이미 완료한 결과 보존을 확인한다. 정책 조합의 Git/Agent 관측은 기존 mock 경계를 사용한다.
- `tests/test_planning_rebase.py`와 publication/recovery 회귀에서 교정 전·중 원격 전진, 저장된 correction 재개와 동일 target 반복 적용 방지를 검증한다. 기존 실제 Git 보존 회귀를 재사용해 staged/unstaged/untracked 변경·삭제·세션과 journal target의 보존 효과를 확인하며 불필요한 실제 게시 반복을 추가하지 않는다.
- Manager 전체 verify는 신규/기존 계획의 권한 검사·planning validation 복구·rebase/stash/checkpoint·원격 게시 경합과 유지보수 단일 평가 회귀를 유지한다. Reviewer는 최신 입력 확인이 검증/교정보다 앞서고 writer와 rebase가 겹치지 않는지, 무관한 변경의 무재호출과 관련 변경의 재평가 근거를 읽기 전용으로 확인한다.

## Progress

- [x] 2026-10-09 `26993760f5c55048d8a72483ef7c116292e9fd52`의 활성 명세 전문과 catalog, planning 진입·작성/검증·충돌 교정·게시 및 관련 테스트를 정적으로 대조했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] Planner 종료/교정 경계 갱신·재평가 및 설명 정합화
- [x] Worker targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review
- [ ] 사용자 Merge

## Decision Log

- 현재 coordinator는 `_run_planner_turn`에서 `_validate_planner_result`로 바로 진행한다. `_prepare_workspace`는 진입 갱신·관련 결과 재평가를 이미 지원하지만 일반 작성 종료 직후 다시 적용되지 않는다. 게시 경계의 원격 확인은 오래된 게시를 막아도 앞선 검증/교정의 최신 입력을 보장하지 않는다. 보존된 planning 오류 해제의 제한된 fetch 역시 일반 종료 경계의 대체가 아니다.
- `PlanningConflictCorrection.resolve`는 입력 target과 저장된 correction으로 호출·재개하고 교정 writer 전후 최신 원격 입력을 별도로 확인하지 않는다. 기존 rebase journal의 target을 임의 교체하지 않고 완료 뒤 갱신으로 연결하는 책임이 필요하다.
- G-242의 실행 단계 DOCUMENT_RESULT/PR 경합, G-243의 수동 소유 보호, G-189/G-199의 로컬 수용·원격 통합·게시 감사와 범위를 구분한다. 이 계획은 Planner 단계의 원격 입력 순서만 보완한다. 기존 계획의 목표·경로·검증·dependency를 바꾸지 않으므로 mutable READY 개정은 필요하지 않다.
- complexity medium: 기존 관련성 판단·workspace 갱신·journal·게시 검사를 단계 dispatch와 교정 재개에 연결하는 책임 경계가 명확하다. risk medium: 기존 소유권·보존·게시 검사를 유지하는 범위에서 실패 영향은 불필요한 모델/검증 교정과 최신 입력 반영 지연이며 기존 journal·보류/재개로 복구한다. 입력별 호출 순서·결과 보존·경합 재개를 검증한다.
- 2026-10-09의 보존된 verify summary는 `f05cb9c6dc831530cd3b34f05ee2c4ca0994d5a3` 기준이다. 이후 planning publication 및 assessment 테스트 변경이 있으므로 이를 현재 revision 전체나 새 경계 조건의 성공으로 기록하지 않는다. 선택된 unittest adapter·기존 수집/결과 계약을 유지한다.
